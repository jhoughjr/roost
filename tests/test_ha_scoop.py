"""ha-scoop → pulse row contract.

pulse stores hourly means keyed on (device, hour) and rejects any row whose
timestamp is not exactly on an hour. A row that misses that boundary is not an
error anyone sees: the POST still returns 200, the row lands in `skipped`, and
the chart quietly has a hole. These tests pin the shaping so that can't drift.
"""
import importlib.util
import os
import unittest

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


def load_ha_scoop():
    """Import bin/ha-scoop.py by path — the hyphen makes it un-importable.

    Safe only because the module re-execs into the venv under __main__ alone;
    were that at module level this import would replace the test process.
    """
    spec = importlib.util.spec_from_file_location("ha_scoop", os.path.join(BIN, "ha-scoop.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ha_scoop = load_ha_scoop()

ENTITIES = {
    "room": "sensor.room_power_current_consumption",
    "fridge": "sensor.mini_fridge_current_consumption",
}
HOUR = 3600


class TestRowsFromResult(unittest.TestCase):
    def test_maps_entities_to_labels(self):
        """Labels, not entity ids — they must match ROOST_TAPO_DEVICES."""
        rows = ha_scoop.rows_from_result(
            {"sensor.room_power_current_consumption": [{"start": 1785751200000, "mean": 678.1}]},
            ENTITIES,
        )
        self.assertEqual(rows, [{"n": "room", "t": 1785751200, "w": 678.1}])

    def test_milliseconds_become_seconds(self):
        """HA reports epoch ms; pulse's sample clock is seconds."""
        rows = ha_scoop.rows_from_result(
            {"sensor.room_power_current_consumption": [{"start": 1785751200000, "mean": 1.0}]},
            ENTITIES,
        )
        self.assertEqual(rows[0]["t"], 1785751200)

    def test_every_row_lands_on_an_hour(self):
        """The contract pulse enforces. A half-hour timezone must not slip through."""
        offset = 1785751200000 + 1800 * 1000  # a :30 bucket
        rows = ha_scoop.rows_from_result(
            {"sensor.room_power_current_consumption": [
                {"start": 1785751200000, "mean": 1.0},
                {"start": offset, "mean": 2.0},
                {"start": 1785751200000 + 61000, "mean": 3.0},
            ]},
            ENTITIES,
        )
        self.assertEqual(len(rows), 3)
        for r in rows:
            self.assertEqual(r["t"] % HOUR, 0, f"{r} is not on an hour boundary")

    def test_unknown_entities_are_dropped(self):
        """HA exposes ~80 entities; only the configured ones are ours to store."""
        rows = ha_scoop.rows_from_result(
            {"sensor.something_unrelated": [{"start": 1785751200000, "mean": 5.0}]},
            ENTITIES,
        )
        self.assertEqual(rows, [])

    def test_null_means_are_skipped_not_zeroed(self):
        """An hour HA has no mean for is absent, not 0 W.

        Recording it as zero would claim the room drew nothing, which is the
        same lie the 'offline' chip told: a gap in knowledge rendered as a
        confident measurement.
        """
        rows = ha_scoop.rows_from_result(
            {"sensor.room_power_current_consumption": [
                {"start": 1785751200000, "mean": None},
                {"start": 1785754800000, "mean": 12.5},
            ]},
            ENTITIES,
        )
        self.assertEqual(rows, [{"n": "room", "t": 1785754800, "w": 12.5}])

    def test_sorted_by_time_then_label(self):
        rows = ha_scoop.rows_from_result(
            {
                "sensor.room_power_current_consumption": [{"start": 1785754800000, "mean": 2.0}],
                "sensor.mini_fridge_current_consumption": [{"start": 1785751200000, "mean": 1.0}],
            },
            ENTITIES,
        )
        self.assertEqual([r["n"] for r in rows], ["fridge", "room"])
        self.assertTrue(all(rows[i]["t"] <= rows[i + 1]["t"] for i in range(len(rows) - 1)))

    def test_zero_is_kept(self):
        """0 W is a reading (an idle fridge), distinct from an absent one."""
        rows = ha_scoop.rows_from_result(
            {"sensor.mini_fridge_current_consumption": [{"start": 1785751200000, "mean": 0.0}]},
            ENTITIES,
        )
        self.assertEqual(rows, [{"n": "fridge", "t": 1785751200, "w": 0.0}])


if __name__ == "__main__":
    unittest.main()
