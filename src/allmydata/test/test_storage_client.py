"""
Tests for allmydata.storage_client.
"""

from __future__ import annotations

from json import (
    loads,
)
import hashlib
from typing import Union, Any

from hyperlink import DecodedURL
from fixtures import (
    TempDir,
)
from testtools.content import (
    text_content,
)
from testtools.matchers import (
    MatchesAll,
    IsInstance,
    MatchesStructure,
    Equals,
    Is,
    AfterPreprocessing,
)

from zope.interface import (
    implementer,
)
from zope.interface.verify import (
    verifyObject,
)

from hyperlink import (
    URL,
)

import attr

from twisted.internet.interfaces import (
    IStreamClientEndpoint,
)
from twisted.application.service import (
    Service,
)

from twisted.trial import unittest
from twisted.internet.defer import (
    Deferred,
    inlineCallbacks,
)
from twisted.python.filepath import (
    FilePath,
)
from twisted.internet.task import Clock

from foolscap.api import (
    Tub,
)
from foolscap.ipb import (
    IConnectionHintHandler,
)

from allmydata.util.deferredutil import MultiFailure

from .no_network import LocalWrapper
from .common import (
    EMPTY_CLIENT_CONFIG,
    SyncTestCase,
    AsyncTestCase,
    UseTestPlugins,
    UseNode,
    SameProcessStreamEndpointAssigner,
    MemoryIntroducerClient,
)
from .common_web import (
    do_http,
)
from .storage_plugin import (
    DummyStorageClient,
)
from allmydata.webish import (
    WebishServer,
)
from allmydata.util import base32, yamlutil
from allmydata.storage_client import (
    IFoolscapStorageServer,
    NativeStorageServer,
    HTTPNativeStorageServer,
    StorageFarmBroker,
    _FoolscapStorage,
    _NullStorage,
    _pick_a_http_server,
    ANONYMOUS_STORAGE_NURLS,
)
from ..storage.server import (
    StorageServer,
)
from ..client import config_from_string

from allmydata.interfaces import (
    IConnectionStatus,
    IStorageServer,
)

SOME_FURL = "pb://abcde@nowhere/fake"


class NativeStorageServerWithVersion(NativeStorageServer):  # type: ignore  # tahoe-lafs/ticket/3573
    def __init__(self, version):
        # note: these instances won't work for anything other than
        # get_available_space() because we don't upcall
        self.version = version
    def get_version(self):
        return self.version


class TestNativeStorageServer(unittest.TestCase):
    def test_get_available_space_new(self):
        nss = NativeStorageServerWithVersion(
            { b"http://allmydata.org/tahoe/protocols/storage/v1":
                { b"maximum-immutable-share-size": 111,
                  b"available-space": 222,
                }
            })
        self.failUnlessEqual(nss.get_available_space(), 222)

    def test_get_available_space_old(self):
        nss = NativeStorageServerWithVersion(
            { b"http://allmydata.org/tahoe/protocols/storage/v1":
                { b"maximum-immutable-share-size": 111,
                }
            })
        self.failUnlessEqual(nss.get_available_space(), 111)

    def test_missing_nickname(self):
        ann = {"anonymous-storage-FURL": "pb://w2hqnbaa25yw4qgcvghl5psa3srpfgw3@tcp:127.0.0.1:51309/vucto2z4fxment3vfxbqecblbf6zyp6x",
               "permutation-seed-base32": "w2hqnbaa25yw4qgcvghl5psa3srpfgw3",
               }
        nss = NativeStorageServer(b"server_id", ann, None, {}, EMPTY_CLIENT_CONFIG)
        self.assertEqual(nss.get_nickname(), "")


class GetConnectionStatus(unittest.TestCase):
    """
    Tests for ``NativeStorageServer.get_connection_status``.
    """
    def test_unrecognized_announcement(self):
        """
        When ``NativeStorageServer`` is constructed with a storage announcement it
        doesn't recognize, its ``get_connection_status`` nevertheless returns
        an object which provides ``IConnectionStatus``.
        """
        # Pretty hard to recognize anything from an empty announcement.
        ann = {}
        nss = NativeStorageServer(b"server_id", ann, Tub, {}, EMPTY_CLIENT_CONFIG)
        nss.start_connecting(lambda: None)
        connection_status = nss.get_connection_status()
        self.assertTrue(IConnectionStatus.providedBy(connection_status))


class UnrecognizedAnnouncement(unittest.TestCase):
    """
    Tests for handling of announcements that aren't recognized and don't use
    *anonymous-storage-FURL*.

    Recognition failure is created by making up something completely novel for
    these tests.  In real use, recognition failure would most likely come from
    an announcement generated by a storage server plugin which is not loaded
    in the client.
    """
    ann = {
        u"name": u"tahoe-lafs-testing-v1",
        u"any-parameter": 12345,
    }
    server_id = b"abc"

    def _tub_maker(self, overrides):
        return Service()

    def native_storage_server(self):
        """
        Make a ``NativeStorageServer`` out of an unrecognizable announcement.
        """
        return NativeStorageServer(
            self.server_id,
            self.ann,
            self._tub_maker,
            {},
            EMPTY_CLIENT_CONFIG,
        )

    def test_no_exceptions(self):
        """
        ``NativeStorageServer`` can be instantiated with an unrecognized
        announcement.
        """
        self.native_storage_server()

    def test_start_connecting(self):
        """
        ``NativeStorageServer.start_connecting`` does not raise an exception.
        """
        server = self.native_storage_server()
        server.start_connecting(None)

    def test_stop_connecting(self):
        """
        ``NativeStorageServer.stop_connecting`` does not raise an exception.
        """
        server = self.native_storage_server()
        server.start_connecting(None)
        server.stop_connecting()

    def test_try_to_connect(self):
        """
        ``NativeStorageServer.try_to_connect`` does not raise an exception.
        """
        server = self.native_storage_server()
        server.start_connecting(None)
        server.try_to_connect()

    def test_various_data_methods(self):
        """
        The data accessors of ``NativeStorageServer`` that depend on the
        announcement do not raise an exception.
        """
        server = self.native_storage_server()
        server.get_permutation_seed()
        server.get_name()
        server.get_longname()
        server.get_tubid()
        server.get_lease_seed()
        server.get_foolscap_write_enabler_seed()
        server.get_nickname()



class PluginMatchedAnnouncement(SyncTestCase):
    """
    Tests for handling by ``NativeStorageServer`` of storage server
    announcements that are handled by an ``IFoolscapStoragePlugin``.
    """
    @inlineCallbacks
    def make_node(self, introducer_furl, storage_plugin, plugin_config):
        """
        Create a client node with the given configuration.

        :param bytes introducer_furl: The introducer furl with which to
            configure the client.

        :param bytes storage_plugin: The name of a storage plugin to enable.

        :param dict[bytes, bytes] plugin_config: Configuration to supply to
            the enabled plugin.  May also be ``None`` for no configuration
            section (distinct from ``{}`` which creates an empty configuration
            section).
        """
        tempdir = TempDir()
        self.useFixture(tempdir)
        self.basedir = FilePath(tempdir.path)
        self.basedir.child(u"private").makedirs()
        self.useFixture(UseTestPlugins())

        self.node_fixture = self.useFixture(UseNode(
            plugin_config,
            storage_plugin,
            self.basedir,
            introducer_furl,
        ))
        self.config = self.node_fixture.config
        self.node = yield self.node_fixture.create_node()
        [self.introducer_client] = self.node.introducer_clients


    def publish(self, server_id, announcement, introducer_client):
        for subscription in introducer_client.subscribed_to:
            if subscription.service_name == u"storage":
                subscription.cb(
                    server_id,
                    announcement,
                    *subscription.args,
                    **subscription.kwargs
                )

    def get_storage(self, server_id, node):
        storage_broker = node.get_storage_broker()
        native_storage_server = storage_broker.servers[server_id]
        return native_storage_server._storage

    def set_rref(self, server_id, node, rref):
        storage_broker = node.get_storage_broker()
        native_storage_server = storage_broker.servers[server_id]
        native_storage_server._rref = rref

    @inlineCallbacks
    def test_ignored_non_enabled_plugin(self):
        """
        An announcement that could be matched by a plugin that is not enabled is
        not matched.
        """
        yield self.make_node(
            introducer_furl=SOME_FURL,
            storage_plugin="tahoe-lafs-dummy-v1",
            plugin_config=None,
        )
        server_id = b"v0-abcdef"
        ann = {
            u"service-name": u"storage",
            u"storage-options": [{
                # notice how the announcement is for a different storage plugin
                # than the one that is enabled.
                u"name": u"tahoe-lafs-dummy-v2",
                u"storage-server-FURL": SOME_FURL,
            }],
        }
        self.publish(server_id, ann, self.introducer_client)
        storage = self.get_storage(server_id, self.node)
        self.assertIsInstance(storage, _NullStorage)

    @inlineCallbacks
    def test_enabled_plugin(self):
        """
        An announcement that could be matched by a plugin that is enabled with
        configuration is matched and the plugin's storage client is used.
        """
        plugin_config = {
            "abc": "xyz",
        }
        plugin_name = "tahoe-lafs-dummy-v1"
        yield self.make_node(
            introducer_furl=SOME_FURL,
            storage_plugin=plugin_name,
            plugin_config=plugin_config,
        )
        server_id = b"v0-abcdef"
        ann = {
            u"service-name": u"storage",
            u"storage-options": [{
                # and this announcement is for a plugin with a matching name
                u"name": plugin_name,
                u"storage-server-FURL": SOME_FURL,
            }],
        }
        self.publish(server_id, ann, self.introducer_client)
        storage = self.get_storage(server_id, self.node)
        self.assertTrue(
            verifyObject(
                IFoolscapStorageServer,
                storage,
            ),
        )
        expected_rref = object()
        # Can't easily establish a real Foolscap connection so fake the result
        # of doing so...
        self.set_rref(server_id, self.node, expected_rref)
        self.expectThat(
            storage.storage_server,
            MatchesAll(
                IsInstance(DummyStorageClient),
                MatchesStructure(
                    get_rref=AfterPreprocessing(
                        lambda get_rref: get_rref(),
                        Is(expected_rref),
                    ),
                    configuration=Equals(plugin_config),
                    announcement=Equals({
                        u'name': plugin_name,
                        u'storage-server-FURL': u'pb://abcde@nowhere/fake',
                    }),
                ),
            ),
        )

    @inlineCallbacks
    def test_enabled_no_configuration_plugin(self):
        """
        An announcement that could be matched by a plugin that is enabled with no
        configuration is matched and the plugin's storage client is used.
        """
        plugin_name = "tahoe-lafs-dummy-v1"
        yield self.make_node(
            introducer_furl=SOME_FURL,
            storage_plugin=plugin_name,
            plugin_config=None,
        )
        server_id = b"v0-abcdef"
        ann = {
            u"service-name": u"storage",
            u"storage-options": [{
                # and this announcement is for a plugin with a matching name
                u"name": plugin_name,
                u"storage-server-FURL": SOME_FURL,
            }],
        }
        self.publish(server_id, ann, self.introducer_client)
        storage = self.get_storage(server_id, self.node)
        self.addDetail("storage", text_content(str(storage)))
        self.expectThat(
            storage.storage_server,
            MatchesAll(
                IsInstance(DummyStorageClient),
                MatchesStructure(
                    configuration=Equals({}),
                ),
            ),
        )


class FoolscapStorageServers(unittest.TestCase):
    """
    Tests for implementations of ``IFoolscapStorageServer``.
    """
    def test_null_provider(self):
        """
        Instances of ``_NullStorage`` provide ``IFoolscapStorageServer``.
        """
        self.assertTrue(
            verifyObject(
                IFoolscapStorageServer,
                _NullStorage(),
            ),
        )

    def test_foolscap_provider(self):
        """
        Instances of ``_FoolscapStorage`` provide ``IFoolscapStorageServer``.
        """
        @implementer(IStorageServer)
        class NotStorageServer(object):
            pass
        self.assertTrue(
            verifyObject(
                IFoolscapStorageServer,
                _FoolscapStorage.from_announcement(
                    b"server-id",
                    SOME_FURL,
                    {u"permutation-seed-base32": base32.b2a(b"permutationseed")},
                    NotStorageServer(),
                ),
            ),
        )


class StoragePluginWebPresence(AsyncTestCase):
    """
    Tests for the web resources ``IFoolscapStorageServer`` plugins may expose.
    """
    @inlineCallbacks
    def setUp(self):
        super(StoragePluginWebPresence, self).setUp()

        self.useFixture(UseTestPlugins())

        self.port_assigner = SameProcessStreamEndpointAssigner()
        self.port_assigner.setUp()
        self.addCleanup(self.port_assigner.tearDown)
        self.storage_plugin = u"tahoe-lafs-dummy-v1"

        from twisted.internet import reactor
        _, webport_endpoint = self.port_assigner.assign(reactor)
        tubport_location, tubport_endpoint = self.port_assigner.assign(reactor)

        tempdir = TempDir()
        self.useFixture(tempdir)
        self.basedir = FilePath(tempdir.path)
        self.basedir.child(u"private").makedirs()
        self.node_fixture = self.useFixture(UseNode(
            plugin_config={
                "web": "1",
            },
            node_config={
                # We don't really need the main Tub listening but if we
                # disable it then we also have to disable storage (because
                # config validation policy).
                "tub.port": tubport_endpoint,
                "tub.location": tubport_location,
                "web.port": str(webport_endpoint),
            },
            storage_plugin=self.storage_plugin,
            basedir=self.basedir,
            introducer_furl=SOME_FURL,
        ))
        self.node = yield self.node_fixture.create_node()
        self.webish = self.node.getServiceNamed(WebishServer.name)
        self.node.startService()
        self.addCleanup(self.node.stopService)
        self.port = self.webish.getPortnum()

    @inlineCallbacks
    def test_plugin_resource_path(self):
        """
        The plugin's resource is published at */storage-plugins/<plugin name>*.
        """
        url = u"http://127.0.0.1:{port}/storage-plugins/{plugin_name}".format(
            port=self.port,
            plugin_name=self.storage_plugin,
        ).encode("utf-8")
        result = yield do_http("get", url)
        self.assertThat(loads(result), Equals({"web": "1"}))

    @inlineCallbacks
    def test_plugin_resource_persistent_across_requests(self):
        """
        The plugin's resource is loaded and then saved and re-used for future
        requests.
        """
        url = URL(
            scheme=u"http",
            host=u"127.0.0.1",
            port=self.port,
            path=(
                u"storage-plugins",
                self.storage_plugin,
                u"counter",
            ),
        ).to_text().encode("utf-8")
        values = {
            loads((yield do_http("get", url)))[u"value"],
            loads((yield do_http("get", url)))[u"value"],
        }
        self.assertThat(
            values,
            # If the counter manages to go up then the state stuck around.
            Equals({1, 2}),
        )


_aCertPEM = Tub().myCertificate.dumpPEM()
def new_tub():
    """
    Make a new ``Tub`` with a hard-coded private key.
    """
    # Use a private key / certificate generated by Tub how it wants.  But just
    # re-use the same one every time so we don't waste a lot of time
    # generating them over and over in the tests.
    return Tub(certData=_aCertPEM)


def make_broker(tub_maker=None):
    """
    Create a ``StorageFarmBroker`` with the given tub maker and an empty
    client configuration.
    """
    if tub_maker is None:
        tub_maker = lambda handler_overrides: new_tub()
    return StorageFarmBroker(True, tub_maker, EMPTY_CLIENT_CONFIG)


@implementer(IStreamClientEndpoint)
@attr.s
class SpyEndpoint(object):
    """
    Observe and record connection attempts.

    :ivar list _append: A callable that accepts two-tuples.  For each
        attempted connection, it will be called with ``Deferred`` that was
        returned and the ``Factory`` that was passed in.
    """
    _append = attr.ib()

    def connect(self, factory):
        """
        Record the connection attempt.

        :return: A ``Deferred`` that ``SpyEndpoint`` will not fire.
        """
        d = Deferred()
        self._append((d, factory))
        return d


@implementer(IConnectionHintHandler)  # type: ignore # warner/foolscap#78
@attr.s
class SpyHandler(object):
    """
    A Foolscap connection hint handler for the "spy" hint type.  Connections
    are handled by just observing and recording them.

    :ivar list _connects: A list containing one element for each connection
        attempted with this handler.  Each element is a two-tuple of the
        ``Deferred`` that was returned from ``connect`` and the factory that
        was passed to ``connect``.
    """
    _connects = attr.ib(default=attr.Factory(list))

    def hint_to_endpoint(self, hint, reactor, update_status):
        return (SpyEndpoint(self._connects.append), hint)


class TestStorageFarmBroker(unittest.TestCase):

    def test_static_servers(self):
        broker = make_broker()

        key_s = b'v0-1234-1'
        servers_yaml = """\
storage:
  v0-1234-1:
    ann:
      anonymous-storage-FURL: {furl}
      permutation-seed-base32: aaaaaaaaaaaaaaaaaaaaaaaa
""".format(furl=SOME_FURL)
        servers = yamlutil.safe_load(servers_yaml)
        permseed = base32.a2b(b"aaaaaaaaaaaaaaaaaaaaaaaa")
        broker.set_static_servers(servers["storage"])
        self.failUnlessEqual(len(broker._static_server_ids), 1)
        s = broker.servers[key_s]
        self.failUnlessEqual(s.announcement,
                             servers["storage"]["v0-1234-1"]["ann"])
        self.failUnlessEqual(s.get_serverid(), key_s)
        self.assertEqual(s.get_permutation_seed(), permseed)

        # if the Introducer announces the same thing, we're supposed to
        # ignore it

        ann2 = {
            "service-name": "storage",
            "anonymous-storage-FURL": "pb://{}@nowhere/fake2".format(str(base32.b2a(b"1"), "utf-8")),
            "permutation-seed-base32": "bbbbbbbbbbbbbbbbbbbbbbbb",
        }
        broker._got_announcement(key_s, ann2)
        s2 = broker.servers[key_s]
        self.assertIdentical(s2, s)
        self.assertEqual(s2.get_permutation_seed(), permseed)

    def test_upgrade_from_foolscap_to_http(self):
        """
        When an announcement is initially Foolscap but then switches to HTTP,
        HTTP is used, assuming HTTP is enabled.
        """
        tub_maker = lambda _: new_tub()
        config = config_from_string(
            "/dev/null", "", "[client]\nforce_foolscap = False\n"
        )
        broker = StorageFarmBroker(True, tub_maker, config)
        broker.startService()
        key_s = b'v0-1234-1'

        ones = str(base32.b2a(b"1"), "utf-8")
        initial_announcement = {
            "service-name": "storage",
            "anonymous-storage-FURL": f"pb://{ones}@nowhere/fake2",
            "permutation-seed-base32": "bbbbbbbbbbbbbbbbbbbbbbbb",
        }
        broker._got_announcement(key_s, initial_announcement)
        initial_service = broker.servers[key_s]
        self.assertIsInstance(initial_service, NativeStorageServer)
        self.assertTrue(initial_service.running)
        self.assertIdentical(initial_service.parent, broker)

        http_announcement = {
            "service-name": "storage",
            "anonymous-storage-FURL": f"pb://{ones}@nowhere/fake2",
            "permutation-seed-base32": "bbbbbbbbbbbbbbbbbbbbbbbb",
            ANONYMOUS_STORAGE_NURLS: [f"pb://{ones}@nowhere/fake2#v=1"],
        }
        broker._got_announcement(key_s, http_announcement)
        self.assertFalse(initial_service.running)
        self.assertEqual(initial_service.parent, None)
        new_service = broker.servers[key_s]
        self.assertIsInstance(new_service, HTTPNativeStorageServer)
        self.assertTrue(new_service.running)
        self.assertIdentical(new_service.parent, broker)

        return broker.stopService()

    def test_static_permutation_seed_pubkey(self):
        broker = make_broker()
        server_id = b"v0-4uazse3xb6uu5qpkb7tel2bm6bpea4jhuigdhqcuvvse7hugtsia"
        k = b"4uazse3xb6uu5qpkb7tel2bm6bpea4jhuigdhqcuvvse7hugtsia"
        ann = {
            "anonymous-storage-FURL": SOME_FURL,
        }
        broker.set_static_servers({server_id.decode("ascii"): {"ann": ann}})
        s = broker.servers[server_id]
        self.assertEqual(s.get_permutation_seed(), base32.a2b(k))

    def test_static_permutation_seed_explicit(self):
        broker = make_broker()
        server_id = b"v0-4uazse3xb6uu5qpkb7tel2bm6bpea4jhuigdhqcuvvse7hugtsia"
        k = b"w5gl5igiexhwmftwzhai5jy2jixn7yx7"
        ann = {
            "anonymous-storage-FURL": SOME_FURL,
            "permutation-seed-base32": k,
        }
        broker.set_static_servers({server_id.decode("ascii"): {"ann": ann}})
        s = broker.servers[server_id]
        self.assertEqual(s.get_permutation_seed(), base32.a2b(k))

    def test_static_permutation_seed_hashed(self):
        broker = make_broker()
        server_id = b"unparseable"
        ann = {
            "anonymous-storage-FURL": SOME_FURL,
        }
        broker.set_static_servers({server_id.decode("ascii"): {"ann": ann}})
        s = broker.servers[server_id]
        self.assertEqual(s.get_permutation_seed(),
                         hashlib.sha256(server_id).digest())

    @inlineCallbacks
    def test_threshold_reached(self):
        """
        ``StorageFarmBroker.when_connected_enough`` returns a ``Deferred`` which
        only fires after the ``StorageFarmBroker`` has established at least as
        many connections as requested.
        """
        introducer = MemoryIntroducerClient(
            new_tub(),
            SOME_FURL,
            b"",
            None,
            None,
            None,
            None,
        )
        new_tubs = []
        def make_tub(*args, **kwargs):
            return new_tubs.pop()
        broker = make_broker(make_tub)
        # Start the broker so that it will start Tubs attached to it so they
        # will attempt to make connections as necessary so that we can observe
        # those connections.
        broker.startService()
        self.addCleanup(broker.stopService)
        done = broker.when_connected_enough(5)
        broker.use_introducer(introducer)
        # subscribes to "storage" to learn of new storage nodes
        [subscribe] = introducer.subscribed_to
        self.assertEqual(
            subscribe.service_name,
            "storage",
        )
        got_announcement = subscribe.cb

        data = {
            "service-name": "storage",
            "anonymous-storage-FURL": None,
            "permutation-seed-base32": "aaaaaaaaaaaaaaaaaaaaaaaa",
        }

        def add_one_server(x):
            data["anonymous-storage-FURL"] = "pb://%s@spy:nowhere/fake" % (str(base32.b2a(b"%d" % x), "ascii"),)
            tub = new_tub()
            connects = []
            spy = SpyHandler(connects)
            tub.addConnectionHintHandler("spy", spy)
            new_tubs.append(tub)
            got_announcement(b'v0-1234-%d' % x, data)

            self.assertEqual(
                1, len(connects),
                "Expected one connection attempt, got {!r} instead".format(connects),
            )

            # Skip over all the Foolscap negotiation.  It's complex with lots
            # of pieces and I don't want to figure out how to fake
            # it. -exarkun
            native = broker.servers[b"v0-1234-%d" % (x,)]
            rref = LocalWrapper(StorageServer(self.mktemp(), b"x" * 20))
            native._got_connection(rref)

        # first 4 shouldn't trigger connected_threashold
        for x in range(4):
            add_one_server(x)
            self.assertFalse(done.called)

        # ...but the 5th *should* trigger the threshold
        add_one_server(42)

        # so: the OneShotObserverList only notifies via
        # foolscap.eventually() -- which forces the Deferred call
        # through the reactor -- so it's no longer synchronous,
        # meaning that we have to do "real reactor stuff" for the
        # Deferred from when_connected_enough() to actually fire. (or
        # @patch() out the reactor in foolscap.eventually to be a
        # Clock() so we can advance time ourselves, but ... luckily
        # eventually() uses 0 as the timeout currently)

        yield done
        self.assertTrue(done.called)

    def test_should_we_use_http_default(self):
        """Default is to use HTTP."""
        basedir = self.mktemp()
        node_config = config_from_string(basedir, "", "")
        announcement = {ANONYMOUS_STORAGE_NURLS: ["pb://..."]}
        self.assertTrue(
            StorageFarmBroker._should_we_use_http(node_config, announcement)
        )
        # Lacking NURLs, we can't use HTTP:
        self.assertFalse(
            StorageFarmBroker._should_we_use_http(node_config, {})
        )

    def test_should_we_use_http(self):
        """
        If HTTP is allowed, it will only be used if the announcement includes
        some NURLs.
        """
        basedir = self.mktemp()

        no_nurls = {}
        empty_nurls = {ANONYMOUS_STORAGE_NURLS: []}
        has_nurls = {ANONYMOUS_STORAGE_NURLS: ["pb://.."]}

        for force_foolscap, announcement, expected_http_usage in [
                ("false", no_nurls, False),
                ("false", empty_nurls, False),
                ("false", has_nurls, True),
                ("true", empty_nurls, False),
                ("true", no_nurls, False),
                ("true", has_nurls, False),
        ]:
            node_config = config_from_string(
                basedir, "", f"[client]\nforce_foolscap = {force_foolscap}"
            )
            self.assertEqual(
                StorageFarmBroker._should_we_use_http(node_config, announcement),
                expected_http_usage
            )


class PickHTTPServerTests(unittest.SynchronousTestCase):
    """Tests for ``_pick_a_http_server``."""

    def pick_result(self, url_to_results: dict[DecodedURL, tuple[float, Union[Exception, Any]]]) -> Deferred[DecodedURL]:
        """
        Given mapping of URLs to (delay, result), return the URL of the
        first selected server, or None.
        """
        clock = Clock()

        def request(reactor, url):
            delay, value = url_to_results[url]
            result = Deferred()
            def add_result_value():
                if isinstance(value, Exception):
                    result.errback(value)
                else:
                    result.callback(value)
            reactor.callLater(delay, add_result_value)
            return result

        d = _pick_a_http_server(clock, list(url_to_results.keys()), request)
        for i in range(100):
            clock.advance(0.1)
        return d

    def test_first_successful_connect_is_picked(self):
        """
        Given multiple good URLs, the first one that connects is chosen.
        """
        earliest_url = DecodedURL.from_text("http://a")
        latest_url = DecodedURL.from_text("http://b")
        bad_url = DecodedURL.from_text("http://bad")
        result = self.pick_result({
            latest_url: (2, None),
            earliest_url: (1, None),
            bad_url: (0.5, RuntimeError()),
        })
        self.assertEqual(self.successResultOf(result), earliest_url)

    def test_failures_include_all_reasons(self):
        """
        If all the requests fail, ``_pick_a_http_server`` raises a
        ``allmydata.util.deferredutil.MultiFailure``.
        """
        eventually_good_url = DecodedURL.from_text("http://good")
        bad_url = DecodedURL.from_text("http://bad")
        exception1 = RuntimeError()
        exception2 = ZeroDivisionError()
        result = self.pick_result({
            eventually_good_url: (1, exception1),
            bad_url: (0.1, exception2),
        })
        exc = self.failureResultOf(result).value
        self.assertIsInstance(exc, MultiFailure)
        self.assertEqual({f.value for f in exc.failures}, {exception2, exception1})
