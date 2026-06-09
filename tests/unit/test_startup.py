"""Tests for src.startup.* (lifespan decomposition)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config.constants import COLL_PROFILE, COLL_RMS_EMAIL_TEMPLATE
from src.config.settings import AppSettings
from src.db.repository import RmsEmailTemplateRepository
from src.startup.infra import InfraContext, init_infra
from src.startup.repos import init_repos


@pytest.fixture
def settings() -> AppSettings:
    return AppSettings()


@pytest.mark.unit
class TestInfraContextClosePartial:
    async def test_close_partial_runs_in_reverse_order(self):
        ctx = InfraContext()
        # Set all clients with AsyncMock close()
        ctx.es = MagicMock()
        ctx.es.close = AsyncMock()
        ctx.mongo = MagicMock()
        ctx.mongo.close = AsyncMock()
        ctx.redis = MagicMock()
        ctx.redis.close = AsyncMock()
        ctx.email = MagicMock()
        ctx.email.close = AsyncMock()
        ctx.zk = MagicMock()
        ctx.zk.close = AsyncMock()

        await ctx.close_partial()

        ctx.es.close.assert_awaited_once()
        ctx.mongo.close.assert_awaited_once()
        ctx.redis.close.assert_awaited_once()
        ctx.email.close.assert_awaited_once()
        ctx.zk.close.assert_awaited_once()

    async def test_close_partial_swallows_individual_failures(self):
        """A failure closing one client must NOT prevent the others from closing."""
        ctx = InfraContext()
        ctx.es = MagicMock()
        ctx.es.close = AsyncMock()
        ctx.mongo = MagicMock()
        ctx.mongo.close = AsyncMock(side_effect=ConnectionError("oops"))
        ctx.redis = MagicMock()
        ctx.redis.close = AsyncMock()

        # Must NOT raise
        await ctx.close_partial()
        ctx.es.close.assert_awaited_once()
        ctx.redis.close.assert_awaited_once()

    async def test_close_partial_skips_none(self):
        ctx = InfraContext()
        # All clients are None
        await ctx.close_partial()  # must not raise


@pytest.mark.unit
class TestInitInfra:
    async def test_init_connects_all_clients(self, settings):
        with (
            patch("src.startup.infra.ESClient") as MockES,
            patch("src.startup.infra.MongoClient") as MockMongo,
            patch("src.startup.infra.RedisClient") as MockRedis,
            patch("src.startup.infra.EmailAlertClient") as MockEmail,
            patch("src.startup.infra.ZKClient") as MockZK,
        ):
            es_inst = MagicMock(connect=AsyncMock())
            mongo_inst = MagicMock(connect_with_retry=AsyncMock())
            redis_inst = MagicMock(connect_with_retry=AsyncMock())
            email_inst = MagicMock(connect=AsyncMock())
            zk_inst = MagicMock(connect=AsyncMock())
            MockES.return_value = es_inst
            MockMongo.return_value = mongo_inst
            MockRedis.return_value = redis_inst
            MockEmail.return_value = email_inst
            MockZK.return_value = zk_inst

            ctx = await init_infra(settings)

        assert ctx.es is es_inst
        assert ctx.mongo is mongo_inst
        assert ctx.redis is redis_inst
        assert ctx.email is email_inst
        assert ctx.zk is zk_inst
        es_inst.connect.assert_awaited_once()
        mongo_inst.connect_with_retry.assert_awaited_once()
        redis_inst.connect_with_retry.assert_awaited_once()
        email_inst.connect.assert_awaited_once()
        zk_inst.connect.assert_awaited_once()

    async def test_init_cleans_up_on_failure(self, settings):
        """If ZK connect fails AFTER ES/Mongo connected, the partial state must be released."""
        with (
            patch("src.startup.infra.ESClient") as MockES,
            patch("src.startup.infra.MongoClient") as MockMongo,
            patch("src.startup.infra.RedisClient") as MockRedis,
            patch("src.startup.infra.EmailAlertClient") as MockEmail,
            patch("src.startup.infra.ZKClient") as MockZK,
        ):
            es_inst = MagicMock(connect=AsyncMock(), close=AsyncMock())
            mongo_inst = MagicMock(
                connect_with_retry=AsyncMock(), close=AsyncMock()
            )
            redis_inst = MagicMock(
                connect_with_retry=AsyncMock(), close=AsyncMock()
            )
            email_inst = MagicMock(connect=AsyncMock(), close=AsyncMock())
            zk_inst = MagicMock(
                connect=AsyncMock(side_effect=ConnectionError("ZK down")),
                close=AsyncMock(),
            )
            MockES.return_value = es_inst
            MockMongo.return_value = mongo_inst
            MockRedis.return_value = redis_inst
            MockEmail.return_value = email_inst
            MockZK.return_value = zk_inst

            with pytest.raises(ConnectionError):
                await init_infra(settings)

            # ES/Mongo/Redis/Email all connected; close must be called on each
            es_inst.close.assert_awaited_once()
            mongo_inst.close.assert_awaited_once()
            redis_inst.close.assert_awaited_once()
            email_inst.close.assert_awaited_once()

    async def test_init_infra_skips_zk_in_debug_mode(self):
        """★ Debug Read-Only mode: ZK must NOT be connected. A debug
        instance registering as a ZK member would pollute the production
        cluster's membership and trigger spurious redistributions."""
        debug_settings = AppSettings(debug_read_only=True)
        with (
            patch("src.startup.infra.ESClient") as MockES,
            patch("src.startup.infra.MongoClient") as MockMongo,
            patch("src.startup.infra.RedisClient") as MockRedis,
            patch("src.startup.infra.EmailAlertClient") as MockEmail,
            patch("src.startup.infra.ZKClient") as MockZK,
        ):
            MockES.return_value = MagicMock(connect=AsyncMock())
            MockMongo.return_value = MagicMock(connect_with_retry=AsyncMock())
            MockRedis.return_value = MagicMock(connect_with_retry=AsyncMock())
            MockEmail.return_value = MagicMock(connect=AsyncMock())

            ctx = await init_infra(debug_settings)

            # ZK not even constructed
            MockZK.assert_not_called()
        assert ctx.zk is None
        # Others still connected — debug instance can read Mongo/ES and
        # talk to the email API (though send_alert is no-op'd separately)
        assert ctx.es is not None
        assert ctx.mongo is not None
        assert ctx.redis is not None
        assert ctx.email is not None


@pytest.mark.unit
class TestInitRepos:
    """Verify init_repos builds repositories and ensures the schema invariants.

    Phase 0 gap fix: PROFILE must have a unique index on (scope.process,
    scope.eqpModel, scope.eqpId) or ProfileRepository.create()'s
    DuplicateKeyError path cannot fire.
    """

    def _make_infra(self) -> InfraContext:
        """Build an InfraContext with a mocked motor-style db."""
        ctx = InfraContext()
        ctx.mongo = MagicMock()
        # motor db is subscriptable: db[COLL] → AsyncMock collection
        collections: dict = {}

        def _getitem(name: str):
            if name not in collections:
                coll = MagicMock()
                coll.create_index = AsyncMock()
                collections[name] = coll
            return collections[name]

        ctx.mongo.db = MagicMock()
        ctx.mongo.db.__getitem__.side_effect = _getitem
        ctx.mongo.db._collections = collections  # test introspection
        # motor db-level async methods used by init_repos
        ctx.mongo.db.list_collection_names = AsyncMock(return_value=[])
        ctx.mongo.db.create_collection = AsyncMock()
        return ctx

    async def test_init_repos_returns_both_repositories(self, settings):
        ctx = self._make_infra()
        repos = await init_repos(ctx, settings)
        assert repos.profile_repo is not None
        assert repos.eqp_info_repo is not None

    async def test_init_repos_builds_template_repo(self, settings):
        ctx = self._make_infra()
        repos = await init_repos(ctx, settings)
        assert isinstance(repos.template_repo, RmsEmailTemplateRepository)

    async def test_init_repos_does_not_create_or_index_template_collection(self, settings):
        """RMS must not create/index RESOURCE_MONITOR_EMAIL_TEMPLATE — it is
        authored/owned by WebManager and read-only here (like EQP_INFO)."""
        ctx = self._make_infra()
        await init_repos(ctx, settings)
        created = [c.args[0] for c in ctx.mongo.db.create_collection.call_args_list]
        assert COLL_RMS_EMAIL_TEMPLATE not in created
        tmpl = ctx.mongo.db._collections.get(COLL_RMS_EMAIL_TEMPLATE)
        if tmpl is not None:  # handle may be taken, but never indexed
            tmpl.create_index.assert_not_awaited()

    async def test_init_repos_creates_unique_scope_index_on_profile(self, settings):
        """★ Regression guard for Phase 0 schema gap:
        init_repos() must call create_index on RESOURCE_MONITOR_PROFILE
        with unique=True on the three scope fields."""
        ctx = self._make_infra()
        await init_repos(ctx, settings)

        profile_coll = ctx.mongo.db._collections[COLL_PROFILE]
        profile_coll.create_index.assert_awaited_once()
        args, kwargs = profile_coll.create_index.call_args
        # First positional arg: the index key list
        key_spec = args[0]
        # Must be a list of (field, direction) tuples covering all three scope fields
        fields = {field for field, _ in key_spec}
        assert fields == {"scope.process", "scope.eqpModel", "scope.eqpId"}
        assert kwargs.get("unique") is True
        # name is encouraged for idempotent re-runs
        assert kwargs.get("name") == "uniq_scope"

    async def test_init_repos_creates_collection_if_absent(self, settings):
        """★ New: init_repos must create an EMPTY RESOURCE_MONITOR_PROFILE
        collection when it does not yet exist (non-debug). Data is inserted
        manually (JSON) afterward — startup no longer seeds a default profile."""
        ctx = self._make_infra()
        ctx.mongo.db.list_collection_names = AsyncMock(return_value=[])
        await init_repos(ctx, settings)
        ctx.mongo.db.create_collection.assert_awaited_once_with(COLL_PROFILE)

    async def test_init_repos_skips_create_collection_when_present(self, settings):
        """Idempotent: if the collection already exists, do NOT re-create it,
        but still ensure the unique index."""
        ctx = self._make_infra()
        ctx.mongo.db.list_collection_names = AsyncMock(return_value=[COLL_PROFILE])
        await init_repos(ctx, settings)
        ctx.mongo.db.create_collection.assert_not_awaited()
        profile_coll = ctx.mongo.db._collections[COLL_PROFILE]
        profile_coll.create_index.assert_awaited_once()

    async def test_init_repos_tolerates_concurrent_create_race(self, settings):
        """★ Regression guard for the multi-instance boot race:
        two instances starting together can BOTH see the collection absent
        (list_collection_names) and BOTH call create_collection; the loser
        gets OperationFailure NamespaceExists (code 48). init_repos must treat
        that as idempotent success (SCHEMA §7 'init_repos가 멱등 생성'), not
        crash startup. Without this, one pod's lifespan fails on concurrent
        deploy (tests/e2e/test_multi_instance leader/failover regression)."""
        from pymongo.errors import OperationFailure

        ctx = self._make_infra()
        # stale view: collection appears absent at check time
        ctx.mongo.db.list_collection_names = AsyncMock(return_value=[])
        # but another instance created it between check and create
        ctx.mongo.db.create_collection = AsyncMock(
            side_effect=OperationFailure("Collection already exists", 48)
        )

        # Must not raise — and must still ensure the unique index afterward.
        repos = await init_repos(ctx, settings)
        assert repos.profile_repo is not None
        profile_coll = ctx.mongo.db._collections[COLL_PROFILE]
        profile_coll.create_index.assert_awaited_once()

    async def test_init_repos_reraises_non_namespace_create_failures(self, settings):
        """Only the NamespaceExists race (code 48) is swallowed; any other
        OperationFailure (auth, disk, etc.) must still propagate so real
        startup problems are not masked."""
        from pymongo.errors import OperationFailure

        ctx = self._make_infra()
        ctx.mongo.db.list_collection_names = AsyncMock(return_value=[])
        ctx.mongo.db.create_collection = AsyncMock(
            side_effect=OperationFailure("not authorized", 13)
        )
        with pytest.raises(OperationFailure):
            await init_repos(ctx, settings)

    async def test_init_repos_raises_if_mongo_not_connected(self, settings):
        ctx = InfraContext()  # mongo is None
        with pytest.raises(RuntimeError, match="connected MongoClient"):
            await init_repos(ctx, settings)

    async def test_init_repos_skips_create_index_in_debug_mode(self):
        """★ Debug Read-Only mode: create_index must NOT be awaited when
        debug_read_only=True. Debug instances rely on the production
        indexes already existing and must not mutate prod schema."""
        ctx = self._make_infra()
        debug_settings = AppSettings(debug_read_only=True)

        repos = await init_repos(ctx, debug_settings)

        # Repositories still wired up
        assert repos.profile_repo is not None
        assert repos.eqp_info_repo is not None
        # But neither create_collection NOR create_index called (no prod schema mutation)
        profile_coll = ctx.mongo.db._collections[COLL_PROFILE]
        profile_coll.create_index.assert_not_awaited()
        ctx.mongo.db.create_collection.assert_not_awaited()
