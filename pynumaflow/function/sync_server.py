import logging
import logging
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Callable, AsyncIterable, List

import grpc
from google.protobuf import empty_pb2 as _empty_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2

from pynumaflow import setup_logging
from pynumaflow._constants import (
    FUNCTION_SOCK_PATH,
    MAX_MESSAGE_SIZE,
)
from pynumaflow.function import Messages, MessageTs, Datum, Metadata
from pynumaflow.function.proto import udfunction_pb2
from pynumaflow.function.proto import udfunction_pb2_grpc
from pynumaflow.types import NumaflowServicerContext

_LOGGER = setup_logging(__name__)
if os.getenv("PYTHONDEBUG"):
    _LOGGER.setLevel(logging.DEBUG)

UDFMapCallable = Callable[[List[str], Datum], Messages]
UDFMapTCallable = Callable[[List[str], Datum], MessageTs]
UDFReduceCallable = Callable[[List[str], AsyncIterable[Datum], Metadata], Messages]
_PROCESS_COUNT = multiprocessing.cpu_count()
MAX_THREADS = int(os.getenv("MAX_THREADS", 0)) or (_PROCESS_COUNT * 4)


class SyncServerServicer(udfunction_pb2_grpc.UserDefinedFunctionServicer):
    """
    Provides an interface to write a User Defined Function (UDFunction)
    which will be exposed over gRPC.

    Args:
        map_handler: Function callable following the type signature of UDFMapCallable
        mapt_handler: Function callable following the type signature of UDFMapTCallable
        reduce_handler: Function callable following the type signature of UDFReduceCallable
        sock_path: Path to the UNIX Domain Socket
        max_message_size: The max message size in bytes the server can receive and send
        max_threads: The max number of threads to be spawned;
                     defaults to number of processors x4

    Example invocation:
    >>> from typing import Iterator
    >>> from pynumaflow.function import Messages, Message, MessageTs, MessageT, \
    ...     Datum, Metadata, SyncServerServicer
    ... import aiorun
    ...
    >>> def map_handler(key: [str], datum: Datum) -> Messages:
    ...   val = datum.value
    ...   _ = datum.event_time
    ...   _ = datum.watermark
    ...   messages = Messages(Message.to_vtx(key, val))
    ...   return messages
    ...
    >>> def mapt_handler(key: [str], datum: Datum) -> MessageTs:
    ...   val = datum.value
    ...   new_event_time = datetime.time()
    ...   _ = datum.watermark
    ...   message_t_s = MessageTs(MessageT.to_vtx(key, val, new_event_time))
    ...   return message_t_s
    ...
    >>> async def reduce_handler(key: str, datums: Iterator[Datum], md: Metadata) -> Messages:
    ...   interval_window = md.interval_window
    ...   counter = 0
    ...   async for _ in datums:
    ...     counter += 1
    ...   msg = (
    ...       f"counter:{counter} interval_window_start:{interval_window.start} "
    ...       f"interval_window_end:{interval_window.end}"
    ...   )
    ...   return Messages(Message.to_vtx(key, str.encode(msg)))
    ...
    >>> grpc_server = UserDefinedFunctionServicer(
    ...   reduce_handler=reduce_handler,
    ...   mapt_handler=mapt_handler,
    ...   map_handler=map_handler,
    ... )
    >>> aiorun.run(grpc_server.start())
    """

    def __init__(
            self,
            map_handler: UDFMapCallable = None,
            mapt_handler: UDFMapTCallable = None,
            reduce_handler: UDFReduceCallable = None,
            sock_path=FUNCTION_SOCK_PATH,
            max_message_size=MAX_MESSAGE_SIZE,
            max_threads=MAX_THREADS,
    ):
        if not (map_handler or mapt_handler or reduce_handler):
            raise ValueError("Require a valid map/mapt handler and/or a valid reduce handler.")

        self.__map_handler: UDFMapCallable = map_handler
        self.__mapt_handler: UDFMapTCallable = mapt_handler
        self.__reduce_handler: UDFReduceCallable = reduce_handler
        self.sock_path = f"unix://{sock_path}"
        self._max_message_size = max_message_size
        self._max_threads = max_threads
        self.cleanup_coroutines = []
        # Collection for storing strong references to all running tasks.
        # Event loop only keeps a weak reference, which can cause it to
        # get lost during execution.
        self.background_tasks = set()

        self._server_options = [
            ("grpc.max_send_message_length", self._max_message_size),
            ("grpc.max_receive_message_length", self._max_message_size),
        ]

    def MapFn(
            self, request: udfunction_pb2.Datum, context: NumaflowServicerContext
    ) -> udfunction_pb2.DatumList:
        """
        Applies a function to each datum element.
        The pascal case function name comes from the proto udfunction_pb2_grpc.py file.
        """
        # proto repeated field(keys) is of type google._upb._message.RepeatedScalarContainer
        # we need to explicitly convert it to list
        try:
            msgs = self.__map_handler(
                list(request.keys),
                Datum(
                    keys=list(request.keys),
                    value=request.value,
                    event_time=request.event_time.event_time.ToDatetime(),
                    watermark=request.watermark.watermark.ToDatetime(),
                ),
            )
        except Exception as err:
            _LOGGER.critical("UDFError, re-raising the error: %r", err, exc_info=True)
            raise err

        datums = []

        for msg in msgs.items():
            datums.append(udfunction_pb2.Datum(keys=msg.keys, value=msg.value))

        return udfunction_pb2.DatumList(elements=datums)

    def MapTFn(
            self, request: udfunction_pb2.Datum, context: NumaflowServicerContext
    ) -> udfunction_pb2.DatumList:
        """
        Applies a function to each datum element.
        The pascal case function name comes from the generated udfunction_pb2_grpc.py file.
        """

        # proto repeated field(keys) is of type google._upb._message.RepeatedScalarContainer
        # we need to explicitly convert it to list
        try:
            msgts = self.__mapt_handler(
                list(request.keys),
                Datum(
                    keys=list(request.keys),
                    value=request.value,
                    event_time=request.event_time.event_time.ToDatetime(),
                    watermark=request.watermark.watermark.ToDatetime(),
                ),
            )
        except Exception as err:
            _LOGGER.critical("UDFError, re-raising the error: %r", err, exc_info=True)
            raise err

        datums = []
        for msgt in msgts.items():
            event_time_timestamp = _timestamp_pb2.Timestamp()
            event_time_timestamp.FromDatetime(dt=msgt.event_time)
            watermark_timestamp = _timestamp_pb2.Timestamp()
            watermark_timestamp.FromDatetime(dt=request.watermark.watermark.ToDatetime())
            datums.append(
                udfunction_pb2.Datum(
                    keys=list(msgt.keys),
                    value=msgt.value,
                    event_time=udfunction_pb2.EventTime(event_time=event_time_timestamp),
                    watermark=udfunction_pb2.Watermark(watermark=watermark_timestamp),
                )
            )
        return udfunction_pb2.DatumList(elements=datums)

    def ReduceFn(
            self,
            request_iterator: AsyncIterable[udfunction_pb2.Datum],
            context: NumaflowServicerContext,
    ) -> udfunction_pb2.DatumList:
        """
        Applies a reduce function to a datum stream.
        The pascal case function name comes from the proto udfunction_pb2_grpc.py file.
        """
        _LOGGER.error("Reduce not supported on NEW SYNC --")
        # context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        # context.set_details("Reduce Not supported")
        # return udfunction_pb2.DatumList()
        raise ValueError("Reduce Not supported on sync")
        _LOGGER.error("Reduce not supported on SYNC -- 2")

    def IsReady(
            self, request: _empty_pb2.Empty, context: NumaflowServicerContext
    ) -> udfunction_pb2.ReadyResponse:
        """
        IsReady is the heartbeat endpoint for gRPC.
        The pascal case function name comes from the proto udfunction_pb2_grpc.py file.
        """
        return udfunction_pb2.ReadyResponse(ready=True)

    def start(self) -> None:
        """
        Starts the gRPC server on the given UNIX socket with given max threads.
        """
        server = grpc.server(
            ThreadPoolExecutor(max_workers=self._max_threads), options=self._server_options
        )
        _LOGGER.error("SERV1")
        udfunction_pb2_grpc.add_UserDefinedFunctionServicer_to_server(self, server)
        _LOGGER.error("SERV2")
        server.add_insecure_port(self.sock_path)
        _LOGGER.error("SERV3")
        server.start()
        _LOGGER.error("SERV4")
        _LOGGER.info(
            "GRPC Server listening on: %s with max threads: %s", self.sock_path, self._max_threads
        )
        server.wait_for_termination()