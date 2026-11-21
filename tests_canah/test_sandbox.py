"""The sandbox: the Docker runtime for real, and that it starts with the agent."""
import os
import shutil
import subprocess
import tempfile
import unittest

from canah import build_agent, load_settings
from canah.identity import Identity
from canah.memory import open_memory
from canah.sandbox import check

from tests_canah.fakes import ScriptedModel, call, say

DOCKER, WHY = check()
ADMIN = Identity("ana", "admin")


@unittest.skipUnless(DOCKER, f"no Docker here: {WHY}")
class DockerSandbox(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["CANAH_SANDBOX_WORKSPACE"] = f"canah-test-{os.getpid()}"
        from canah.sandbox import open_sandbox
        cls.sb = open_sandbox()

    @classmethod
    def tearDownClass(cls):
        os.environ["CANAH_SANDBOX_KEEP_WORKSPACE"] = "0"
        cls.sb.close()
        os.environ.pop("CANAH_SANDBOX_KEEP_WORKSPACE")
        os.environ.pop("CANAH_SANDBOX_WORKSPACE")

    def test_it_is_not_root_and_cannot_change_the_image(self):
        got = self.sb.execute("id -u; touch /etc/x")
        self.assertTrue(got.output.startswith("1000"))
        self.assertIn("Read-only file system", got.output)

    def test_there_is_no_network(self):
        got = self.sb.execute("python3 -c \"import socket; socket.create_connection(('1.1.1.1', 53), 3)\"")
        self.assertNotEqual(got.exit_code, 0)
        self.assertRegex(got.output, "unreachable|Network|resolution|refused")

    def test_files_round_trip_and_read_agrees(self):
        self.assertIsNone(self.sb.upload_files([("/workspace/t/x.txt", "ü line\n".encode())])[0].error)
        self.assertEqual(self.sb.download_files(["/workspace/t/x.txt"])[0].content, "ü line\n".encode())
        self.assertIn("ü line", self.sb.read("/workspace/t/x.txt").file_data["content"])
        self.assertEqual(self.sb.download_files(["/workspace/nope"])[0].error, "file_not_found")

    def test_a_large_one_line_result_reads_whole(self):
        big = '{"rows": [' + ", ".join(f'{{"i": {i}, "pad": "{"x" * 100}"}}' for i in range(600)) + "]}"
        self.assertIsNone(self.sb.upload_files([("/workspace/t/big.json", big.encode())])[0].error)
        got = self.sb.read("/workspace/t/big.json")
        self.assertIsNone(got.error)                      # was "unexpected server response": execute's cap cut it
        self.assertIn('"i": 599', got.file_data["content"])

    def test_a_long_command_is_killed(self):
        self.assertEqual(self.sb.execute("sleep 20", timeout=2).exit_code, 124)

    def test_an_agent_computes_in_it(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        model = ScriptedModel(script=[call("write_file", file_path="/workspace/d.csv", content="a,1\nb,2\n"),
                                      call("execute", command="awk -F, '{s+=$2} END {print s}' /workspace/d.csv"),
                                      say("The total is 3.")])
        a = build_agent(load_settings(path="/x", home=home, sandbox="none"), [], model=model,
                        memory=open_memory(in_memory=True), sandbox=self.sb)
        r = a.ask("add up the second column", who=ADMIN)
        self.assertEqual(r.text, "The total is 3.")
        self.assertIn("3", model.tool_results()[-1])
        self.assertIn("sandbox", model.system_text())


class StartsWithTheAgent(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)

    @unittest.skipUnless(DOCKER, f"no Docker here: {WHY}")
    def test_it_is_on_by_default_and_execute_is_offered_from_the_first_message(self):
        os.environ["CANAH_SANDBOX_WORKSPACE"] = f"canah-test-start-{os.getpid()}"
        self.addCleanup(os.environ.pop, "CANAH_SANDBOX_WORKSPACE")
        self.assertEqual(load_settings(path="/x", home=self.home).sandbox if "CANAH_SANDBOX" not in os.environ
                         else "docker", "docker")
        model = ScriptedModel(script=[say("ok")])
        a = build_agent(load_settings(path="/x", home=self.home, sandbox="docker"), [], model=model,
                        memory=open_memory(in_memory=True))
        self.assertTrue(a.info["sandbox"].startswith("Docker container"), a.info["sandbox"])
        name = a.sandbox.runtime.container
        self.assertTrue(name)                                         # running before anything was asked
        a.ask("hi", who=ADMIN)
        self.assertIn("execute", model.offered[-1])
        os.environ["CANAH_SANDBOX_KEEP_WORKSPACE"] = "0"
        a.close()
        os.environ.pop("CANAH_SANDBOX_KEEP_WORKSPACE")
        left = subprocess.run(["docker", "--context", "default", "ps", "-q", "--filter", f"name={name}"],
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(left, "")                                    # removed with the agent

    def test_without_docker_the_agent_starts_and_says_so(self):
        os.environ["CANAH_DOCKER_CONTEXT"] = "no-such-context"
        self.addCleanup(os.environ.pop, "CANAH_DOCKER_CONTEXT")
        model = ScriptedModel(script=[say("ok")])
        a = build_agent(load_settings(path="/x", home=self.home, sandbox="docker"), [], model=model,
                        memory=open_memory(in_memory=True))
        self.addCleanup(a.close)
        self.assertTrue(a.info["sandbox"].startswith("none: "), a.info["sandbox"])
        a.ask("hi", who=ADMIN)
        self.assertIn("no sandbox", model.system_text())
        self.assertNotIn("execute", model.offered[-1])

    def test_the_default_setting_is_docker(self):
        from canah.config import Settings
        self.assertEqual(Settings().sandbox, "docker")


if __name__ == "__main__":
    unittest.main()


class JqHyphen(unittest.TestCase):
    def test_a_jq_hyphen_key_error_says_how_to_quote_it(self):
        from canah.sandbox.backend import SandboxBackend

        class R:
            def run(self, argv, timeout):
                return "", 'jq: error: null (null) and string ("object") cannot be subtracted', 5
        sb = SandboxBackend.__new__(SandboxBackend)
        sb.runtime, sb.default_timeout = R(), 30
        out = sb.execute("""jq -c '.nodes[] | select(.node-type == "XPONDER")' /workspace/x.json""").output
        self.assertIn('."node-type"', out)
