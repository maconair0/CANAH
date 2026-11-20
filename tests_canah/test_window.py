"""The context window (in the model's profile, compacted at compact_at) and the KV cache figures."""
import unittest

from langchain_core.messages import AIMessage

from canah import build_agent, load_settings
from canah import window
from canah.memory import open_memory
from canah.ops import Ops

from tests_canah.fakes import ScriptedModel, say


def answer(text, prompt, out=20, cached=None, eval_s=None):
    """A model reply with the usage a provider reports: exact cache figures, or Ollama's timings."""
    usage = {"input_tokens": prompt, "output_tokens": out, "total_tokens": prompt + out}
    meta = {}
    if cached is not None:
        usage["input_token_details"] = {"cache_read": cached, "cache_creation": 0}
    if eval_s is not None:
        meta = {"prompt_eval_count": prompt, "prompt_eval_duration": int(eval_s * 1e9)}
    return AIMessage(content=text, usage_metadata=usage, response_metadata=meta)


class Ollamaish:
    num_ctx, num_predict, profile = 32768, 4096, None


class Window(unittest.TestCase):
    def test_the_window_comes_from_ollamas_num_ctx_less_the_answer(self):
        m = Ollamaish()
        self.assertEqual(window.give_profile(m), 28672)
        self.assertEqual(m.profile, {"max_input_tokens": 28672})

    def test_a_setting_overrides_the_model(self):
        self.assertEqual(window.give_profile(Ollamaish(), 9000), 9000)

    def test_compaction_thresholds_are_the_settings(self):
        import deepagents.middleware.summarization as s
        m = ScriptedModel(script=[])
        m.profile = {"max_input_tokens": 10000}
        with window.compaction(0.7, 0.2):
            d = s.compute_summarization_defaults(m)
        self.assertEqual((d["trigger"], d["keep"]), (("fraction", 0.7), ("fraction", 0.2)))
        self.assertEqual(s.compute_summarization_defaults(m)["trigger"], ("fraction", 0.85))   # restored


class KVCache(unittest.TestCase):
    def test_a_hosted_model_reports_it_exactly(self):
        k = window.cache_of(answer("x", 8000, cached=7600))
        self.assertEqual((k["prompt"], k["cached"], k["exact"]), (8000, 7600, True))

    def test_ollama_is_estimated_from_its_reading_time(self):
        window.cache_of(answer("x", 7000, eval_s=2.0))            # read whole: 3,500 tokens/s
        k = window.cache_of(answer("x", 7200, eval_s=0.06))       # 210 tokens read, the rest from cache
        self.assertFalse(k["exact"])
        self.assertAlmostEqual(k["cached"], 7200 - 210, delta=60)

    def test_ollama_reuses_the_conversation_so_far(self):
        conv = "t-prefix|"
        window.cache_of(answer("first", 7000, out=100, eval_s=2.0), conv)
        k = window.cache_of(answer("second", 8600, eval_s=0.6), conv)        # 7,100 again, 1,500 new
        self.assertEqual(k["cached"], 7100)
        k = window.cache_of(answer("third", 9000, eval_s=9.0), conv)         # far too slow: the cache was lost
        self.assertLess(k["cached"], 8600)

    def test_ops_totals_it(self):
        ops = Ops()
        for hit in (900, 950):
            ops.request(user="u", cache_prompt=1000, cache_hit=hit, context_used=7000, context_window=28000)
        m = ops.metrics()
        self.assertEqual(m["kv_cache_percent"], 92.5)
        self.assertEqual(m["context_percent_median"], 25.0)


class InTheAgent(unittest.TestCase):
    def agent(self, *script, **settings):
        model = ScriptedModel(script=list(script))
        model.profile = {"max_input_tokens": settings.pop("window", 10000)}
        a = build_agent(load_settings(path="/x", sandbox="none", look_first=False, **settings), [], model=model,
                        memory=open_memory(in_memory=True))
        self.addCleanup(a.close)
        return a, model

    def test_each_reply_says_how_full_the_window_is_and_what_the_cache_gave(self):
        a, _ = self.agent(answer("hi", 2500, out=40, cached=2400))
        r = a.ask("hello", thread_id="w1")
        self.assertEqual(r.usage["context"], {"used": 2540, "window": 10000, "percent": 25.4})
        self.assertEqual(r.usage["kv_cache"]["percent"], 96.0)
        self.assertTrue(r.usage["kv_cache"]["exact"])
        self.assertEqual(a.info["context"]["window"], 10000)

    def test_the_agent_can_compact_on_request(self):
        a, _ = self.agent(say("ok"))
        self.assertIn("compact_conversation", [t.name for t in a.graph.nodes["tools"].bound.tools_by_name.values()])

    def test_past_compact_at_the_older_part_is_summarised(self):
        # Turn one fills 90% of a 10,000-token window; turn two starts past 85% and is compacted first. The
        # trigger is the usage the model reported (as Ollama's and hosted models' replies carry it), when the
        # reply names the model's own provider.
        first = answer("noted", 9000, out=10)
        a, model = self.agent(first, say("Summary: the user said hello."), say("done"))
        first.response_metadata["model_provider"] = model._get_ls_params().get("ls_provider")
        a.ask("hello " * 1500, thread_id="w2")   # more than the 10% kept as it is, so something is summarised
        r = a.ask("and now?", thread_id="w2")
        self.assertTrue(r.usage.get("compacted"), r.usage)
        self.assertTrue(any("summar" in str(m[-1].content).lower() or "summar" in str(m[0].content).lower()
                            for m in model.seen[1:2]), "the model was asked for a summary")


if __name__ == "__main__":
    unittest.main()
