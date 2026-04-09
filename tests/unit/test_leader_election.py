"""Tests for src.distributed.leader_election (fire-and-forget Election)."""
import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest

from src.distributed.leader_election import LeaderElection


@pytest.fixture
async def mock_zk():
    """ZKClient stub with a kazoo mock and the running asyncio loop."""
    zk = MagicMock()
    zk.root_path = "/resource-monitor"
    zk.kazoo = MagicMock()
    zk.kazoo.get.return_value = (b"0", None)  # initial epoch=0
    zk.kazoo.set.return_value = None
    zk.kazoo.ensure_path.return_value = None
    zk.loop = asyncio.get_running_loop()
    return zk


@pytest.mark.unit
class TestLeaderElectionStart:
    async def test_start_does_not_block(self, mock_zk):
        """The CRITICAL bug v3 fixed: election.run() must NOT block start()."""
        election = MagicMock()
        # election.run blocks forever until something releases its callback
        block_event = threading.Event()

        def fake_run(callback):
            block_event.wait()  # would block forever in a sync await

        election.run.side_effect = fake_run

        with patch(
            "src.distributed.leader_election.Election", return_value=election
        ):
            le = LeaderElection(mock_zk, instance_id="inst-1")
            # If this hangs, the bug is back. We give it 1s.
            await asyncio.wait_for(le.start(), timeout=1.0)

        # Cleanup
        block_event.set()
        await le.stop()

    async def test_start_ensures_epoch_path(self, mock_zk):
        with patch("src.distributed.leader_election.Election", return_value=MagicMock()):
            le = LeaderElection(mock_zk, instance_id="inst-1")
            await le.start()
            await le.stop()
        mock_zk.kazoo.ensure_path.assert_called_with(
            "/resource-monitor/leader-epoch"
        )


@pytest.mark.unit
class TestBecomeLeaderCallback:
    async def test_on_become_leader_increments_epoch(self, mock_zk):
        """Stored epoch starts at 5, becoming leader writes 6."""
        mock_zk.kazoo.get.return_value = (b"5", None)
        callback_done = threading.Event()
        received_epoch: list[int] = []

        async def on_acquired(epoch: int):
            received_epoch.append(epoch)
            callback_done.set()

        election = MagicMock()

        def fake_run(callback):
            callback()  # synchronously call our handler from this thread
            # then block until stop_event is set so election.run does not return

        election.run.side_effect = fake_run

        with patch(
            "src.distributed.leader_election.Election", return_value=election
        ):
            le = LeaderElection(mock_zk, instance_id="inst-1")
            le.add_on_acquired_callback(on_acquired)
            await le.start()
            # Give the bridged callback time to execute on the loop
            await asyncio.sleep(0.05)
            await le.stop()

        assert received_epoch == [6]
        # Should write epoch=6 to ZK
        set_call = mock_zk.kazoo.set.call_args
        assert set_call.args[0] == "/resource-monitor/leader-epoch"
        assert set_call.args[1] == b"6"

    async def test_epoch_starts_at_zero_when_node_empty(self, mock_zk):
        mock_zk.kazoo.get.return_value = (b"", None)
        election = MagicMock()
        election.run.side_effect = lambda cb: cb()

        with patch(
            "src.distributed.leader_election.Election", return_value=election
        ):
            le = LeaderElection(mock_zk, instance_id="inst-1")
            await le.start()
            await asyncio.sleep(0.05)
            await le.stop()

        assert le.epoch == 1


@pytest.mark.unit
class TestRestartAfterLoss:
    async def test_restart_creates_new_election_object(self, mock_zk):
        """v4: After LOST, the OLD Election is bound to the dead session.
        We must create a NEW Election object on restart."""
        election_objs = []

        def make_election(*_a, **_kw):
            e = MagicMock()
            e.run.side_effect = lambda cb: None  # don't block
            election_objs.append(e)
            return e

        with patch(
            "src.distributed.leader_election.Election",
            side_effect=make_election,
        ):
            le = LeaderElection(mock_zk, instance_id="inst-1")
            await le.start()
            assert len(election_objs) == 1
            await le.restart_after_loss()
            assert len(election_objs) == 2
            assert election_objs[0] is not election_objs[1]
            await le.stop()


@pytest.mark.unit
class TestStop:
    async def test_stop_sets_event(self, mock_zk):
        election = MagicMock()
        election.run.side_effect = lambda cb: None

        with patch(
            "src.distributed.leader_election.Election", return_value=election
        ):
            le = LeaderElection(mock_zk, instance_id="inst-1")
            await le.start()
            assert le._stop_event.is_set() is False
            await le.stop()
            assert le._stop_event.is_set() is True
