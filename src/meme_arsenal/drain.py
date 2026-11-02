"""Private process-lifetime evidence for the community backup helper."""

import json
import os
from pathlib import Path
import uuid

from uvicorn import Server


STATE = Path('/tmp/arsenal-api-drain.json')


class DrainServer(Server):
    def __init__(self, config):
        super().__init__(config)
        self.token = uuid.uuid4().hex
        self.record('starting')

    def record(self, phase):
        # Container-local, never in the reserve; a new process invalidates old proof.
        temporary = STATE.with_suffix('.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump({'token': self.token, 'phase': phase}, output)
        temporary.replace(STATE)

    async def startup(self, sockets=None):
        await super().startup(sockets)
        if self.started and not self.should_exit:
            self.record('ready')

    async def shutdown(self, sockets=None):
        # No deadline: Uvicorn waits for HTTP tasks, including synchronous request
        # threads and yielded dependency teardown, before lifespan shutdown.
        await super().shutdown(sockets)
        if (self.started and not self.force_exit and not self.server_state.tasks
                and not self.server_state.connections
                and not self.lifespan.shutdown_failed):
            self.record('drained')
