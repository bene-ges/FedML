import logging
import time

from ..slave.client_constants import ClientConstants
from ..master.server_constants import ServerConstants
from enum import Enum, unique
import multiprocessing
from multiprocessing import Process, Queue
import queue
from .message_common import FedMLMessageEntity, FedMLStatusEntity
from .message_center import FedMLMessageCenter
import traceback
from .status_manager_protocols import FedMLStatusManager
from .compute_cache_manager import ComputeCacheManager


@unique
class JobStatus(Enum):
    STATUS_OFFLINE = "OFFLINE"
    STATUS_PROVISIONING = "PROVISIONING"
    STATUS_IDLE = "IDLE"
    UPGRADING = "UPGRADING"
    STARTING = "STARTING"
    STATUS_RUNNING = "RUNNING"
    STATUS_STOPPING = "STOPPING"
    STATUS_KILLED = "KILLED"
    STATUS_FAILED = "FAILED"
    STATUS_FINISHED = "FINISHED"
    STATUS_EXCEPTION = "EXCEPTION"

    def __str__(self):
        return self.value

    @classmethod
    def get_job_enum_from_str(cls, job_status_str: str):
        for job_status in cls:
            if job_status.value == job_status_str:
                return job_status
        return cls.STATUS_OFFLINE

    @staticmethod
    def is_job_completed(job_status_str: str):
        if job_status_str == JobStatus.STATUS_FINISHED.value or \
                job_status_str == JobStatus.STATUS_FAILED.value or \
                job_status_str == JobStatus.STATUS_KILLED.value or \
                job_status_str == JobStatus.STATUS_EXCEPTION.value:
            return True

        return False


@unique
class DeviceStatus(Enum):
    STATUS_OFFLINE = "OFFLINE"
    STATUS_PROVISIONING = "PROVISIONING"
    STATUS_IDLE = "IDLE"
    STATUS_UPGRADING = "UPGRADING"
    STATUS_QUEUED = "QUEUED"
    STATUS_INITIALIZING = "INITIALIZING"
    STATUS_TRAINING = "TRAINING"
    STATUS_RUNNING = "RUNNING"
    STATUS_STOPPING = "STOPPING"
    STATUS_KILLED = "KILLED"
    STATUS_FAILED = "FAILED"
    STATUS_EXCEPTION = "EXCEPTION"
    STATUS_FINISHED = "FINISHED"

    def __str__(self):
        return self.value

    @classmethod
    def get_device_enum_from_str(cls, device_status_str: str):
        for device_status in cls:
            if device_status.value == device_status_str:
                return device_status
        return cls.STATUS_OFFLINE


class FedMLStatusCenter(object):
    TOPIC_MASTER_STATUS_PREFIX = "fl_server/flserver_agent_"
    TOPIC_SLAVE_STATUS_PREFIX = "fl_client/flclient_agent_"
    TOPIC_SLAVE_STATUS_TO_MLOPS_PREFIX = "fl_run/fl_client/mlops/status"
    TOPIC_SLAVE_JOB_LAUNCH_PREFIX = "flserver_agent/"
    TOPIC_SLAVE_JOB_LAUNCH_SUFFIX = "/start_train"
    TOPIC_SLAVE_JOB_STOP_PREFIX = "flserver_agent/"
    TOPIC_SLAVE_JOB_STOP_SUFFIX = "/stop_train"

    def __init__(self, message_queue=None):
        self.status_queue = message_queue
        self.job_status_in_slave = dict()
        self.entire_job_status = None
        self.job_status_in_master = dict()
        self.slave_devices_status = dict()
        self.master_devices_status = dict()
        self.status_center_process = None
        self.status_event = None
        self.status_sender_message_center_queue = None
        self.status_listener_message_center_queue = None
        self.status_message_center = None
        self.status_manager_instance = None
        self.status_runner = None

    def __repr__(self):
        return "<{klass} @{id:x} {attrs}>".format(
            klass=self.__class__.__name__,
            id=id(self) & 0xFFFFFF,
            attrs=" ".join("{}={!r}".format(k, v) for k, v in self.__dict__.items()),
        )

    def add_job_status_in_slave(self, device_id, status):
        self.job_status_in_slave[device_id] = self._status_transition(status)

    def add_job_status_in_master(self, device_id, status):
        self.job_status_in_master[device_id] = self._status_transition(status)

    def set_entire_job_status(self, status):
        self.entire_job_status = status

    def add_slave_device_status(self, device_id, status):
        self.slave_devices_status[device_id] = self._status_transition(status)

    def add_master_device_status(self, device_id, status):
        self.master_devices_status[device_id] = self._status_transition(status)

    def get_job_status_in_slave(self, device_id):
        return self.job_status_in_slave.get(device_id, None)

    def get_job_status_in_master(self, device_id):
        return self.job_status_in_master.get(device_id, None)

    def get_entire_job_status(self):
        return self.entire_job_status

    def get_slave_device_status(self, device_id):
        return self.slave_devices_status.get(device_id, None)

    def get_master_device_status(self, device_id):
        return self.master_devices_status.get(device_id, None)

    def _status_transition(self, status):
        transition_status = status
        if self.entire_job_status is not None:
            if self.entire_job_status == ServerConstants.MSG_MLOPS_SERVER_STATUS_FAILED or \
                    self.entire_job_status == ServerConstants.MSG_MLOPS_SERVER_STATUS_FINISHED:
                if status == ClientConstants.MSG_MLOPS_CLIENT_STATUS_FAILED or \
                        status == ClientConstants.MSG_MLOPS_CLIENT_STATUS_FINISHED or \
                        status == ClientConstants.MSG_MLOPS_CLIENT_STATUS_KILLED:
                    transition_status = status
                else:
                    transition_status = ClientConstants.MSG_MLOPS_CLIENT_STATUS_KILLED

        return transition_status

    def get_status_runner(self):
        return None

    def start_status_center(self, sender_message_center_queue=None,
                            listener_message_center_queue=None, is_slave_agent=False):
        self.status_queue = Queue()
        self.status_event = multiprocessing.Event()
        self.status_event.clear()
        self.status_sender_message_center_queue = sender_message_center_queue
        self.status_listener_message_center_queue = listener_message_center_queue
        self.status_runner = self.get_status_runner()
        target_func = self.status_runner.run_status_dispatcher if not is_slave_agent else \
            self.status_runner.run_status_dispatcher_in_slave
        self.status_center_process = Process(
            target=target_func, args=(
                self.status_event, self.status_queue, self.status_sender_message_center_queue,
                self.status_listener_message_center_queue
            )
        )

        self.status_center_process.start()

    def check_message_stop_event(self):
        if self.status_event is not None and self.status_event.is_set():
            logging.info("Received status center stopping event.")
            raise StatusCenterStoppedException("Status center stopped (for sender)")

    def send_message(self, topic, payload, run_id=None):
        message_entity = FedMLMessageEntity(topic=topic, payload=payload, run_id=run_id)
        self.status_queue.put(message_entity.get_message_body())

    def send_message_json(self, topic, payload):
        self.send_message(topic, payload)

    def send_status_message(self, topic, payload):
        message_entity = FedMLMessageEntity(topic=topic, payload=payload)
        self.status_queue.put(message_entity.get_message_body())

    def get_status_queue(self):
        return self.status_queue

    def status_center_process_master_status(self, topic, payload):
        pass

    def status_center_process_slave_status(self, topic, payload):
        pass

    def rebuild_message_center(self, message_center_queue):
        pass

    def rebuild_status_center(self, status_queue):
        pass

    @staticmethod
    def save_job_status(run_id, status):
        ComputeCacheManager.get_instance().set_redis_params()
        ComputeCacheManager.get_instance().get_status_cache().save_job_status(run_id, status)

    @staticmethod
    def save_device_status_in_job(run_id, device_id, status):
        ComputeCacheManager.get_instance().set_redis_params()
        ComputeCacheManager.get_instance().get_status_cache().save_device_status_in_job(run_id, device_id, status)

    def run_status_dispatcher(self, status_event, status_queue,
                              sender_message_center_queue,
                              listener_message_center_queue):
        # Save the parameters
        self.status_event = status_event
        self.status_queue = status_queue
        self.status_sender_message_center_queue = sender_message_center_queue
        self.status_listener_message_center_queue = listener_message_center_queue

        # Rebuild message center
        message_center = None
        if sender_message_center_queue is not None:
            self.rebuild_message_center(sender_message_center_queue)
            message_center = FedMLMessageCenter(
                sender_message_queue=sender_message_center_queue,
                listener_message_queue=listener_message_center_queue
            )

        if sender_message_center_queue is not None:
            self.rebuild_status_center(status_queue)

        # Init status manager instances
        status_manager_instances = dict()

        while True:
            message_entity = None

            # Check if we should stop status dispatcher
            try:
                self.check_message_stop_event()
            except StatusCenterStoppedException as e:
                break

            # Dispatch status messages.
            # noinspection PyBroadException
            try:
                # Get the status message from the queue
                try:
                    message_body = status_queue.get(block=False, timeout=0.1)
                except queue.Empty as e:  # If queue is empty, then break loop
                    message_body = None
                if message_body is None:
                    time.sleep(0.1)
                    continue

                # Build message and status entity
                message_entity = FedMLMessageEntity(message_body=message_body)
                status_entity = FedMLStatusEntity(status_msg_body=message_body)

                # Generate status manager instance
                if status_manager_instances.get(status_entity.run_id) is None:
                    status_manager_instances[status_entity.run_id] = FedMLStatusManager(
                        run_id=status_entity.run_id, edge_id=status_entity.edge_id, status_center=self,
                        message_center=message_center)
                else:
                    status_manager_instances[status_entity.run_id].edge_id = status_entity.edge_id

                # Process the master and slave status.
                if message_entity.topic.startswith(FedMLStatusCenter.TOPIC_MASTER_STATUS_PREFIX):
                    # Process the job status
                    status_manager_instances[status_entity.run_id].status_center_process_master_status(
                        message_entity.topic, message_entity.payload)

                    # Save the job status
                    FedMLStatusCenter.save_job_status(status_entity.run_id, self.get_entire_job_status())

                elif message_entity.topic.startswith(FedMLStatusCenter.TOPIC_SLAVE_STATUS_PREFIX):
                    # Process the slave device status
                    status_manager_instances[status_entity.run_id].status_center_process_slave_status(
                        message_entity.topic, message_entity.payload)

                    # Save the device status in job
                    FedMLStatusCenter.save_device_status_in_job(status_entity.run_id, status_entity.edge_id,
                                                                self.get_job_status_in_slave(status_entity.edge_id))

            except Exception as e:
                if message_entity is not None:
                    logging.info(
                        f"Failed to process the status with topic {message_entity.topic}, "
                        f"payload {message_entity.payload}, {traceback.format_exc()}")
                else:
                    logging.info(f"Failed to process the status: {traceback.format_exc()}")

    def run_status_dispatcher_in_slave(self, status_event, status_queue,
                                       sender_message_center_queue,
                                       listener_message_center_queue):
        # Save the parameters
        self.status_event = status_event
        self.status_queue = status_queue
        self.status_sender_message_center_queue = sender_message_center_queue
        self.status_listener_message_center_queue = listener_message_center_queue

        # Rebuild message center
        message_center = None
        if sender_message_center_queue is not None:
            self.rebuild_message_center(sender_message_center_queue)
            message_center = FedMLMessageCenter(
                sender_message_queue=sender_message_center_queue,
                listener_message_queue=listener_message_center_queue
            )

        if sender_message_center_queue is not None:
            self.rebuild_status_center(status_queue)

        # Init status manager instances
        status_manager_instances = dict()
        job_launch_message_map = dict()

        while True:
            message_entity = None

            # Check if we should stop status dispatcher
            try:
                self.check_message_stop_event()
            except StatusCenterStoppedException as e:
                break

            # Dispatch status messages.
            # noinspection PyBroadException
            try:
                # Get the status message from the queue
                try:
                    message_body = status_queue.get(block=False, timeout=0.1)
                except queue.Empty as e:  # If queue is empty, then break loop
                    message_body = None
                if message_body is None:
                    time.sleep(0.1)
                    continue

                # Build message and status entity
                message_entity = FedMLMessageEntity(message_body=message_body)
                status_entity = FedMLStatusEntity(status_msg_body=message_body)

                # Generate status manager instance
                if status_manager_instances.get(status_entity.run_id) is None:
                    status_manager_instances[status_entity.run_id] = FedMLStatusManager(
                        run_id=status_entity.run_id, edge_id=status_entity.edge_id, status_center=self,
                        message_center=message_center)
                else:
                    status_manager_instances[status_entity.run_id].edge_id = status_entity.edge_id

                # Process the slave status
                if message_entity.topic.startswith(FedMLStatusCenter.TOPIC_SLAVE_STATUS_PREFIX):
                    # Report the slave status to master
                    status_manager_instances[status_entity.run_id]. \
                        status_center_process_slave_status_to_master_in_slave_agent(
                        message_entity.topic, message_entity.payload
                    )
                elif message_entity.topic.startswith(FedMLStatusCenter.TOPIC_SLAVE_STATUS_TO_MLOPS_PREFIX):
                    # Report slave status to mlops (Active/IDLE message)
                    status_manager_instances[status_entity.run_id]. \
                        status_center_process_slave_status_to_mlops_in_slave_agent(
                        message_entity.topic, message_entity.payload
                    )
                elif (message_entity.topic.startswith(FedMLStatusCenter.TOPIC_SLAVE_JOB_LAUNCH_PREFIX) and
                      message_entity.topic.endswith(FedMLStatusCenter.TOPIC_SLAVE_JOB_LAUNCH_SUFFIX)):
                    # Async request the job status from master when launching the job
                    job_launch_message_map[status_entity.run_id] = {"topic": message_entity.topic,
                                                                    "payload": message_entity.payload}
                    # status_manager_instances[status_entity.run_id]. \
                    #     status_center_request_job_status_from_master_in_slave_agent(
                    #     message_entity.topic, message_entity.payload
                    # )
                elif (message_entity.topic.startswith(FedMLStatusCenter.TOPIC_SLAVE_JOB_STOP_PREFIX) and
                      message_entity.topic.endswith(FedMLStatusCenter.TOPIC_SLAVE_JOB_STOP_SUFFIX)):
                    # Cleanup when stopped the job
                    if job_launch_message_map.get(status_entity.run_id, None) is not None:
                        job_launch_message_map.pop(status_entity.run_id)

            except Exception as e:
                if message_entity is not None:
                    logging.info(
                        f"Failed to process the status with topic {message_entity.topic}, "
                        f"payload {message_entity.payload}, {traceback.format_exc()}")
                else:
                    logging.info(f"Failed to process the status: {traceback.format_exc()}")

    def register_job_launch_message(self, topic, payload):
        message_entity = FedMLMessageEntity(topic=topic, payload=payload)
        self.status_queue.put(message_entity.get_message_body())

    def register_job_stop_message(self, topic, payload):
        message_entity = FedMLMessageEntity(topic=topic, payload=payload)
        self.status_queue.put(message_entity.get_message_body())

    @staticmethod
    def rebuild_status_center_from_queue(status_queue):
        status_center = FedMLStatusCenter(message_queue=status_queue)
        return status_center


class StatusCenterStoppedException(Exception):
    """ Status center stopped. """
    pass
