import queue
import sys
import threading
import time
from abc import ABC
from copy import deepcopy
from select import select
from socket import socket

from coap_core.coap_packet.coap_config import CoapType, CoapCodeFormat, CoapOptionDelta
from coap_core.coap_packet.coap_packet import CoapPacket
from coap_core.coap_packet.coap_templates import CoapTemplates
from coap_core.coap_resource.resource import Resource
from coap_core.coap_resource.resource_manager import ResourceManager
from coap_core.coap_transaction.coap_transaction_pool import CoapTransactionPool
from coap_core.coap_utilities.coap_logger import logger, LogColor
from coap_core.coap_worker.coap_worker import CoapWorker


class CoapWorkerPool(ABC):
    CURRENT_TOKEN = -1

    @staticmethod
    def __gen_token() -> bytes:
        CoapWorkerPool.CURRENT_TOKEN += 1
        CoapWorkerPool.CURRENT_TOKEN = CoapWorkerPool.CURRENT_TOKEN
        return int(CoapWorkerPool.CURRENT_TOKEN).to_bytes()

    @staticmethod
    def __verify_format(task) -> bool:
        if (task.version != 1
                or not CoapType.is_valid(task.message_type)
                or not CoapCodeFormat.is_valid(task.code)
                or not CoapOptionDelta.is_valid(task.options)):
            return False

        return True

    def __init__(self, skt: socket, resource: Resource, receive_queue=None):
        self.name = f"WorkerPoll"

        self.__is_running = True

        self._shared_work = {}
        self._failed_requests = {}

        self._socket = skt

        self.__workers: list[CoapWorker] = []

        self.__valid_coap_packets = queue.Queue()
        if receive_queue:
            self._received_packets = receive_queue
        else:
            self._received_packets = queue.Queue()

        self.__idle_event = threading.Event()
        self.__transaction_event = threading.Event()

        self.__stop_event = threading.Event()

        self.__max_queue_size = 10000
        self.__allowed_idle_time = 60

        self.__background_threads: list[threading.Thread] = [
            threading.Thread(target=self.__coap_format_filter, name="PoolThread"),
            threading.Thread(target=self.__handle_transactions, name="PoolThread"),
            threading.Thread(target=self.__handle_workers, name="PoolThread"),
            threading.Thread(target=self.__stop_safety, name="PoolThread")
        ]

        self.__transaction_pool = CoapTransactionPool()
        ResourceManager().add_default_resource(resource)

    def _add_background_thread(self, thread: threading.Thread):
        self.__background_threads.append(thread)

    def __choose_worker(self) -> CoapWorker:
        light_loaded_workers = filter(lambda worker: not worker.is_heavily_loaded(), self.__workers)
        available_workers = filter(lambda worker: worker.get_queue_size() < self.__max_queue_size, light_loaded_workers)
        chosen_worker = min(available_workers, default=None, key=lambda x: x.get_queue_size())

        if not chosen_worker:
            chosen_worker = CoapWorker(self._shared_work)
            chosen_worker.start()

            self.__workers.append(chosen_worker)

        return chosen_worker

    @logger
    def __handle_transactions(self):
        while self.__is_running:
            self.__transaction_event.wait(timeout=1)
            CoapTransactionPool().solve_transactions()
            self.__transaction_event.clear()

    @logger
    def __handle_workers(self):
        while self.__is_running:
            self.__idle_event.wait(timeout=60)
            for worker in self.__workers:
                if worker.get_idle_time() > self.__allowed_idle_time and len(self.__workers) > 1:
                    self.__workers.remove(worker)
                    worker.stop()
            self.__idle_event.clear()

    @logger
    def __coap_format_filter(self):

        while self.__is_running:
            data: tuple[bytes, tuple] = self._received_packets.get()
            packet = CoapPacket.decode(data[0], data[1], self._socket)
            if CoapWorkerPool.__verify_format(packet):
                match packet.message_type:

                    case CoapType.CON.value:
                        if not self.__transaction_pool.is_overall_transaction_failed(packet):
                            if CoapCodeFormat.is_method(packet.code):  # GET PUT POST DELETE FETCH
                                ack = CoapTemplates.EMPTY_ACK.value_with(packet.token, packet.message_id)
                                ack.options[packet.get_option_code()] = packet.options[packet.get_option_code()]
                            elif packet.code == CoapCodeFormat.SUCCESS_CONTENT.value():  # CONTENT
                                ack = CoapTemplates.SUCCESS_CONTINUE_ACK.value_with(packet.token, packet.message_id)
                                ack.options[packet.get_option_code()] = packet.options[packet.get_option_code()]
                            else:
                                ack = CoapTemplates.EMPTY_ACK.value_with(packet.token, packet.message_id)

                            self._socket.sendto(ack.encode(), packet.sender_ip_port)
                            if packet.work_id() not in self._shared_work:
                                self.__choose_worker().submit_task(packet)
                                self._shared_work[packet.work_id()] = time.time()
                            else:
                                logger.debug(f"{self.name} Packet duplicated: \n {packet}")

                    case CoapType.ACK.value:
                        CoapTransactionPool().finish_transaction(packet)

                    case CoapType.RST.value:
                        self._failed_requests[packet.general_work_id()] = time.time()
                        self.__transaction_pool.set_overall_transaction_failure(packet)
                        self.__transaction_pool.finish_overall_transaction(packet)
                        logger.log(f"! Warning: {CoapCodeFormat.get_field_name(packet.code)}", LogColor.YELLOW)

                    case _:
                        pass
            else:
                logger.debug(f"{self.name} Invalid coap format: \n {packet.__repr__()}")

                invalid_format = CoapTemplates.NON_COAP_FORMAT.value_with(packet.token, packet.message_id)
                invalid_format.code = CoapCodeFormat.SERVER_ERROR_INTERNAL_SERVER_ERROR.value()

                self._socket.sendto(invalid_format.encode(), packet.sender_ip_port)

    @logger
    def listen(self):
        self.start()

        while self.__is_running:
            try:
                active_socket, _, _ = select([self._socket], [], [], 1)

                if active_socket:
                    data, address = self._socket.recvfrom(1152)
                    self._received_packets.put((data, address))

            except Exception:
                pass

        self.stop()

    def _handle_internal_task(self, task: CoapPacket):
        task.token = CoapWorkerPool.__gen_token()
        if task.needs_internal_computation:
            chosen_worker = CoapWorker(self._shared_work)
            chosen_worker.start()
            chosen_worker.submit_task(task)

            self.__workers.append(chosen_worker)
        self._shared_work[task.work_id()] = time.time()
        self.__transaction_pool.add_transaction(task)

    def start(self):
        for thread in self.__background_threads:
            thread.start()

    def stop(self):
        self.__stop_event.set()

    @logger
    def __stop_safety(self):
        self.__stop_event.wait()

        self.__is_running = False

        for worker in self.__workers:
            if worker != threading.current_thread():
                worker.stop()

        for worker in self.__workers:
            if worker != threading.current_thread():
                worker.join()

        self._socket.close()

        sys.exit(0)