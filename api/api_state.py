import os
from json import load


class APIState:
    def __init__(self, process_handler, logger):
        self.logger = logger
        self.process_handler = process_handler
        self.status_file_path = "/healthcheck/running_processes.json"
        os.makedirs(os.path.dirname(self.status_file_path), exist_ok=True)
        self.service_status = self._load_status_from_file()
        self.shutdown_in_progress = set()

    def _load_status_from_file(self):
        try:
            with open(self.status_file_path, "r") as f:
                data = load(f)
                return data
        except FileNotFoundError:
            self.logger.debug(
                f"Status file {self.status_file_path} not found. Initializing empty status."
            )
            return {}
        except Exception as e:
            self.logger.error(f"Error loading status file: {e}")
            return {}

    def get_status(self, process_name):
        running_processes = self._load_status_from_file()

        def normalize(name):
            return name.replace(" ", "").replace("/ ", "/").strip().lower()

        normalized_input = normalize(process_name)
        if normalized_input == "dumbapi" or normalized_input == "dmbapi":
            return "running"
        for stored_name in running_processes:
            normalized_stored_name = normalize(stored_name)
            if normalized_input == normalized_stored_name:
                return "running"
        return "stopped"

    def debug_state(self):
        self.logger.info(f"Current APIState: {self.service_status}")
