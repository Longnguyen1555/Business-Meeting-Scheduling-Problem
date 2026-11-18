"""Checks of recorded schedule comparisons; no optimizer is invoked."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from Cross_Generation_Analysis import objective_priority_tables


def record(config, idle, *, sha="a" * 64, repetition=1, status="OPTIMAL"):
    counts = (max(idle) - min(idle), max(idle), sum(idle))
    vector = {
        "compact_objective_ir": counts[:1],
        "waiting_ir_is_compact_f": (counts[0], counts[2]),
        "compact_objective_ir_im_is": counts,
    }[config]
    return dict(planned_configuration_id=config, instance_sha256=sha,
                instance_content_id="b2b-" + sha[:16], repetition=str(repetition), status=status,
                objective_participants="1,2", participant_internal_idle_slots=",".join(map(str, idle)),
                idle_range_pstar=str(counts[0]), maximum_internal_idle_slots=str(counts[1]),
                total_internal_idle_slots=str(counts[2]), objective_vector=",".join(map(str, vector)))


class ObjectivePriorityAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.matched = [record("compact_objective_ir", (2, 4), repetition=1, status="TIMEOUT"),
                        record("compact_objective_ir", (2, 4), repetition=2),
                        record("compact_objective_ir", (0, 2), repetition=3),
                        record("compact_objective_ir_im_is", (0, 2))]
        self.priority = [record("waiting_ir_is_compact_f", (0, 2))]

    def test_selection_signs_and_common_set_membership(self):
        # A second source instance has no optimal IR-IM-IS run and must be excluded.
        for config in ("compact_objective_ir", "compact_objective_ir_im_is"):
            self.matched.append(record(config, (0, 0), sha="b" * 64,
                                       status="TIMEOUT" if config.endswith("im_is") else "OPTIMAL"))
        self.priority.append(record("waiting_ir_is_compact_f", (0, 0), sha="b" * 64))
        summary, pairs, membership = objective_priority_tables(self.matched, self.priority)
        total = next(r for r in summary if r["left_objective"] == "IR-IS"
                     and r["right_objective"] == "IR" and r["measure"] == "total_internal_idle_slots")
        self.assertEqual((total["instances"], total["lower"], total["equal"], total["higher"]), (1, 1, 0, 0))
        difference = next(r for r in pairs if r["left_objective"] == "IR-IS"
                          and r["right_objective"] == "IR" and r["measure"] == "total_internal_idle_slots")
        self.assertEqual(difference["difference"], -4)
        self.assertEqual(difference["right_repetition"], "2")
        excluded = next(r for r in membership if not r["included"])
        self.assertEqual(excluded["exclusion_reason"], "IR-IM-IS:no_OPTIMAL_run")
        self.assertTrue(all(r["lower"] + r["equal"] + r["higher"] == r["instances"] for r in summary))

    def test_duplicate_run_and_conflicting_instance_hash_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            objective_priority_tables(self.matched, self.priority * 2)
        altered = deepcopy(self.priority)
        altered[0]["instance_sha256"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "conflicting SHA"):
            objective_priority_tables(self.matched, altered)

    def test_participant_metrics_and_objective_vectors_are_checked(self):
        for field, value, message in (("total_internal_idle_slots", "3", "idle totals"),
                                       ("objective_vector", "2,3", "objective vector"),
                                       ("objective_participants", "2,1", "participant sets")):
            altered = deepcopy(self.priority)
            altered[0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, message):
                objective_priority_tables(self.matched, altered)


if __name__ == "__main__":
    unittest.main()
