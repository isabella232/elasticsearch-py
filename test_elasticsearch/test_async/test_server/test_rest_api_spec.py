# Licensed to Elasticsearch B.V under one or more agreements.
# Elasticsearch B.V licenses this file to you under the Apache 2.0 License.
# See the LICENSE file in the project root for more information

"""
Dynamically generated set of TestCases based on set of yaml files decribing
some integration tests. These files are shared among all official Elasticsearch
clients.
"""
import pytest
import re
from os import walk, environ
from os.path import exists, join, dirname, pardir, relpath
import yaml
from shutil import rmtree
import warnings
import inspect

from elasticsearch import TransportError, RequestError, ElasticsearchDeprecationWarning
from elasticsearch.compat import string_types
from elasticsearch.helpers.test import _get_version

pytestmark = pytest.mark.asyncio

# some params had to be changed in python, keep track of them so we can rename
# those in the tests accordingly
PARAMS_RENAMES = {"type": "doc_type", "from": "from_"}

# mapping from catch values to http status codes
CATCH_CODES = {"missing": 404, "conflict": 409, "unauthorized": 401}

# test features we have implemented
IMPLEMENTED_FEATURES = {
    "gtelte",
    "stash_in_path",
    "headers",
    "catch_unauthorized",
    "default_shards",
    "warnings",
}

# broken YAML tests on some releases
SKIP_TESTS = {
    # [interval] on [date_histogram] is deprecated, use [fixed_interval] or [calendar_interval] in the future.
    "search/aggregation/230_composite[6]",
    "search/aggregation/250_moving_fn[1]",
    # fails by not returning 'search'?
    "search/320_disallow_queries[2]",
    "search/40_indices_boost[1]",
    # ?q= fails
    "explain/30_query_string[0]",
    "count/20_query_string[0]",
    # index template issues
    "indices/put_template/10_basic[0]",
    "indices/put_template/10_basic[1]",
    "indices/put_template/10_basic[2]",
    "indices/put_template/10_basic[3]",
    "indices/put_template/10_basic[4]",
    # depends on order of response JSON which is random
    "indices/simulate_index_template/10_basic[1]",
}

XPACK_FEATURES = None
ES_VERSION = None

YAML_DIR = environ.get(
    "TEST_ES_YAML_DIR",
    join(
        dirname(__file__),
        pardir,
        pardir,
        pardir,
        pardir,
        "elasticsearch",
        "rest-api-spec",
        "src",
        "main",
        "resources",
        "rest-api-spec",
        "test",
    ),
)


YAML_TEST_SPECS = []

if exists(YAML_DIR):
    # find all the test definitions in yaml files ...
    for path, _, files in walk(YAML_DIR):
        for filename in files:
            if not filename.endswith((".yaml", ".yml")):
                continue

            filepath = join(path, filename)
            with open(filepath) as f:
                tests = list(yaml.load_all(f, Loader=yaml.SafeLoader))

            setup_code = None
            teardown_code = None
            run_codes = []
            for i, test in enumerate(tests):
                for test_name, definition in test.items():
                    if test_name == "setup":
                        setup_code = definition
                    elif test_name == "teardown":
                        teardown_code = definition
                    else:
                        run_codes.append((i, definition))

            for i, run_code in run_codes:
                src = {"setup": setup_code, "run": run_code, "teardown": teardown_code}
                # Pytest already replaces '.' and '_' with '/' so we do
                # it ourselves so UI and 'SKIP_TESTS' match.
                pytest_param_id = (
                    "%s[%d]" % (relpath(filepath, YAML_DIR).rpartition(".")[0], i)
                ).replace(".", "/")

                if pytest_param_id in SKIP_TESTS:
                    src["skip"] = True

                YAML_TEST_SPECS.append(pytest.param(src, id=pytest_param_id))


async def await_if_coro(x):
    if inspect.iscoroutine(x):
        return await x
    return x


class YamlRunner:
    def __init__(self, client):
        self.client = client
        self.last_response = None

        self._run_code = None
        self._setup_code = None
        self._teardown_code = None
        self._state = {}

    def use_spec(self, test_spec):
        self._setup_code = test_spec.pop("setup", None)
        self._run_code = test_spec.pop("run", None)
        self._teardown_code = test_spec.pop("teardown")

    async def setup(self):
        if self._setup_code:
            await self.run_code(self._setup_code)

    async def teardown(self):
        if self._teardown_code:
            await self.run_code(self._teardown_code)

        for repo, definition in (
            await self.client.snapshot.get_repository(repository="_all")
        ).items():
            await self.client.snapshot.delete_repository(repository=repo)
            if definition["type"] == "fs":
                rmtree(
                    "/tmp/%s" % definition["settings"]["location"], ignore_errors=True
                )

        # stop and remove all ML stuff
        if await self._feature_enabled("ml"):
            await self.client.ml.stop_datafeed(datafeed_id="*", force=True)
            for feed in (await self.client.ml.get_datafeeds(datafeed_id="*"))[
                "datafeeds"
            ]:
                await self.client.ml.delete_datafeed(datafeed_id=feed["datafeed_id"])

            await self.client.ml.close_job(job_id="*", force=True)
            for job in (await self.client.ml.get_jobs(job_id="*"))["jobs"]:
                await self.client.ml.delete_job(
                    job_id=job["job_id"], wait_for_completion=True, force=True
                )

        # stop and remove all Rollup jobs
        if await self._feature_enabled("rollup"):
            for rollup in (await self.client.rollup.get_jobs(id="*"))["jobs"]:
                await self.client.rollup.stop_job(
                    id=rollup["config"]["id"], wait_for_completion=True
                )
                await self.client.rollup.delete_job(id=rollup["config"]["id"])

    async def es_version(self):
        global ES_VERSION
        if ES_VERSION is None:
            version_string = (await self.client.info())["version"]["number"]
            if "." not in version_string:
                return ()
            version = version_string.strip().split(".")
            ES_VERSION = tuple(int(v) if v.isdigit() else 999 for v in version)
        return ES_VERSION

    async def run(self):
        try:
            await self.setup()
            await self.run_code(self._run_code)
        finally:
            await self.teardown()

    async def run_code(self, test):
        """ Execute an instruction based on it's type. """
        print(test)
        for action in test:
            assert len(action) == 1
            action_type, action = list(action.items())[0]

            if hasattr(self, "run_" + action_type):
                await await_if_coro(getattr(self, "run_" + action_type)(action))
            else:
                raise InvalidActionType(action_type)

    async def run_do(self, action):
        api = self.client
        headers = action.pop("headers", None)
        catch = action.pop("catch", None)
        warn = action.pop("warnings", ())
        assert len(action) == 1

        method, args = list(action.items())[0]
        args["headers"] = headers

        # locate api endpoint
        for m in method.split("."):
            assert hasattr(api, m)
            api = getattr(api, m)

        # some parameters had to be renamed to not clash with python builtins,
        # compensate
        for k in PARAMS_RENAMES:
            if k in args:
                args[PARAMS_RENAMES[k]] = args.pop(k)

        # resolve vars
        for k in args:
            args[k] = self._resolve(args[k])

        warnings.simplefilter("always", category=ElasticsearchDeprecationWarning)
        with warnings.catch_warnings(record=True) as caught_warnings:
            try:
                self.last_response = await api(**args)
            except Exception as e:
                if not catch:
                    raise
                self.run_catch(catch, e)
            else:
                if catch:
                    raise AssertionError(
                        "Failed to catch %r in %r." % (catch, self.last_response)
                    )

        # Filter out warnings raised by other components.
        caught_warnings = [
            str(w.message)
            for w in caught_warnings
            if w.category == ElasticsearchDeprecationWarning
        ]

        # Sorting removes the issue with order raised. We only care about
        # if all warnings are raised in the single API call.
        if sorted(warn) != sorted(caught_warnings):
            raise AssertionError(
                "Expected warnings not equal to actual warnings: expected=%r actual=%r"
                % (warn, caught_warnings)
            )

    def run_catch(self, catch, exception):
        if catch == "param":
            assert isinstance(exception, TypeError)
            return

        assert isinstance(exception, TransportError)
        if catch in CATCH_CODES:
            assert CATCH_CODES[catch] == exception.status_code
        elif catch[0] == "/" and catch[-1] == "/":
            assert (
                re.search(catch[1:-1], exception.error + " " + repr(exception.info)),
                "%s not in %r" % (catch, exception.info),
            ) is not None
        self.last_response = exception.info

    async def run_skip(self, skip):
        global IMPLEMENTED_FEATURES

        if "features" in skip:
            features = skip["features"]
            if not isinstance(features, (tuple, list)):
                features = [features]
            for feature in features:
                if feature in IMPLEMENTED_FEATURES:
                    continue
                pytest.skip("feature '%s' is not supported" % feature)

        if "version" in skip:
            version, reason = skip["version"], skip["reason"]
            if version == "all":
                pytest.skip(reason)
            min_version, max_version = version.split("-")
            min_version = _get_version(min_version) or (0,)
            max_version = _get_version(max_version) or (999,)
            if min_version <= (await self.es_version()) <= max_version:
                pytest.skip(reason)

    def run_gt(self, action):
        for key, value in action.items():
            value = self._resolve(value)
            assert self._lookup(key) > value

    def run_gte(self, action):
        for key, value in action.items():
            value = self._resolve(value)
            assert self._lookup(key) >= value

    def run_lt(self, action):
        for key, value in action.items():
            value = self._resolve(value)
            assert self._lookup(key) < value

    def run_lte(self, action):
        for key, value in action.items():
            value = self._resolve(value)
            assert self._lookup(key) <= value

    def run_set(self, action):
        for key, value in action.items():
            value = self._resolve(value)
            self._state[value] = self._lookup(key)

    def run_is_false(self, action):
        try:
            value = self._lookup(action)
        except AssertionError:
            pass
        else:
            assert value in ("", None, False, 0)

    def run_is_true(self, action):
        value = self._lookup(action)
        assert value not in ("", None, False, 0)

    def run_length(self, action):
        for path, expected in action.items():
            value = self._lookup(path)
            expected = self._resolve(expected)
            assert expected == len(value)

    def run_match(self, action):
        for path, expected in action.items():
            value = self._lookup(path)
            expected = self._resolve(expected)

            if (
                isinstance(expected, string_types)
                and expected.startswith("/")
                and expected.endswith("/")
            ):
                expected = re.compile(expected[1:-1], re.VERBOSE | re.MULTILINE)
                assert expected.search(value), "%r does not match %r" % (
                    value,
                    expected,
                )
            else:
                assert expected == value, "%r does not match %r" % (value, expected)

    def _resolve(self, value):
        # resolve variables
        if isinstance(value, string_types) and value.startswith("$"):
            value = value[1:]
            assert value in self._state
            value = self._state[value]
        if isinstance(value, string_types):
            value = value.strip()
        elif isinstance(value, dict):
            value = dict((k, self._resolve(v)) for (k, v) in value.items())
        elif isinstance(value, list):
            value = list(map(self._resolve, value))
        return value

    def _lookup(self, path):
        # fetch the possibly nested value from last_response
        value = self.last_response
        if path == "$body":
            return value
        path = path.replace(r"\.", "\1")
        for step in path.split("."):
            if not step:
                continue
            step = step.replace("\1", ".")
            step = self._resolve(step)
            if step.isdigit() and step not in value:
                step = int(step)
                assert isinstance(value, list)
                assert len(value) > step
            else:
                assert step in value
            value = value[step]
        return value

    async def _feature_enabled(self, name):
        global XPACK_FEATURES, IMPLEMENTED_FEATURES
        if XPACK_FEATURES is None:
            try:
                xinfo = await self.client.xpack.info()
                XPACK_FEATURES = set(
                    f for f in xinfo["features"] if xinfo["features"][f]["enabled"]
                )
                IMPLEMENTED_FEATURES.add("xpack")
            except RequestError:
                XPACK_FEATURES = set()
                IMPLEMENTED_FEATURES.add("no_xpack")
        return name in XPACK_FEATURES


@pytest.fixture(scope="function")
def runner(async_client):
    return YamlRunner(async_client)


@pytest.mark.parametrize("test_spec", YAML_TEST_SPECS)
async def test_rest_api_spec(test_spec, runner):
    if test_spec.get("skip", False):
        pytest.skip("Manually skipped in 'SKIP_TESTS'")
    runner.use_spec(test_spec)
    await runner.run()


class InvalidActionType(Exception):
    pass