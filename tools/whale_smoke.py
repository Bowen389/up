"""Bounded integration check for the uploaded Whale V7; no production state."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone

import whale_monitor_v7 as whale


def run():
    output = Path(os.environ.get("WHALE_SMOKE_OUTPUT", "whale-smoke-output"))
    output.mkdir(parents=True, exist_ok=True)
    summary = {
        "mode": "WHALE_SMOKE_ONLY",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "offline": "pending", "live": "not_started",
        "note": "Isolated test, CD_RECOVER only. No production baseline or trading signals.",
    }
    keys = ["WHALE_SELF_TEST", "WHALE_LOCAL_ROOT", "WHALE_RANK_USERS",
            "WHALE_MAX_WHALES_PER_ROUND", "WHALE_FIRST_SCAN_DYNAMICS_PAGES",
            "WHALE_DYNAMICS_MAX_PAGES", "WHALE_CATCHUP_MAX_PAGES",
            "WHALE_FIRST_SCAN_REPORT_HISTORY", "WHALE_EXTRA_GOODS"]
    original = {k: os.environ.get(k) for k in keys}
    original_request = whale.CSQAQClient._request
    try:
        with tempfile.TemporaryDirectory(prefix="whale-offline-") as root:
            os.environ.update(WHALE_SELF_TEST="1", WHALE_LOCAL_ROOT=root,
                              WHALE_FIRST_SCAN_REPORT_HISTORY="0", WHALE_EXTRA_GOODS="")
            counts = []
            for _ in range(4):
                with contextlib.redirect_stdout(io.StringIO()):
                    cfg = whale.Config.from_env()
                    whale.run_once(cfg, whale.DemoClient(cfg.root))
                counts.append(json.loads((Path(root) / "reports/new_cd_items.json").read_text())["count"])
            if counts != [0, 2, 2, 0]:
                raise RuntimeError("Offline incremental result mismatch")
            summary.update(offline="passed", offline_new_counts=counts)
            print("OFFLINE_OK new_counts=0,2,2,0", flush=True)

        if not os.environ.get("CSQAQ_TOKEN", "").strip():
            summary["live"] = "blocked_missing_CSQAQ_TOKEN"
            raise RuntimeError("CSQAQ_TOKEN is missing from repository Actions secrets")

        with tempfile.TemporaryDirectory(prefix="whale-live-") as root:
            os.environ.update(WHALE_SELF_TEST="0", WHALE_LOCAL_ROOT=root,
                              WHALE_RANK_USERS="2", WHALE_MAX_WHALES_PER_ROUND="2",
                              WHALE_FIRST_SCAN_DYNAMICS_PAGES="1", WHALE_DYNAMICS_MAX_PAGES="1",
                              WHALE_CATCHUP_MAX_PAGES="1", WHALE_FIRST_SCAN_REPORT_HISTORY="0",
                              WHALE_EXTRA_GOODS="")
            # Explicit known item, not a claim that it is currently on the hot list.
            cfg = whale.Config.from_env()
            cfg.source = "snapshot"
            whale.atomic_json(cfg.root / "data/top30_latest.json",
                              [{"good_id": "144", "name": "smoke-item-144", "rank": 1}])
            requests = 0
            def bounded_request(self, path, payload=None):
                nonlocal requests
                requests += 1
                if requests > 12:
                    raise whale.AuthError("Smoke HTTP request budget exceeded")
                return original_request(self, path, payload)
            whale.CSQAQClient._request = bounded_request
            client = whale.CSQAQClient(os.environ["CSQAQ_TOKEN"].strip(), cfg)
            print("LIVE_START item=144 accounts<=2 pages_per_account=1 http_budget=12", flush=True)
            with contextlib.redirect_stdout(io.StringIO()):
                client.bind_ip()
                for round_no in range(2):
                    whale.run_once(cfg, client)
                    report = json.loads((cfg.root / "reports/latest_report.json").read_text())
                    summary.setdefault("live_rounds", []).append({
                        "round": round_no + 1,
                        "new_event_rows": report.get("new_event_rows"),
                    })
                    # run_once can publish a report even when every account failed.
                    pool = whale.load_pool(cfg.root)
                    _, scans = whale.load_snapshot(cfg.root)
                    if not pool or len(scans) != min(2, len(pool)):
                        raise RuntimeError("Live account scan incomplete")
                    stats = json.loads((cfg.root / "reports/debug/scan_page_stats.json").read_text())
                    if len(stats) != min(2, len(pool)):
                        raise RuntimeError("Live second-round scan incomplete")
                    summary["live_rounds"][-1].update(
                        accounts_scanned=len(stats),
                        coverage_risk_accounts=sum(bool(s.get("coverage_risk")) for s in stats))
            summary.update(live="passed", http_requests=requests)
            print("LIVE_OK two bounded rounds; first scan establishes baseline", flush=True)
    except Exception as exc:
        if summary["live"] == "not_started":
            summary["live"] = "failed_or_blocked"
        elif summary["live"] != "blocked_missing_CSQAQ_TOKEN":
            summary["live"] = "failed"
        # Do not publish raw API responses, user IDs, inventory, or credentials.
        summary["error_type"] = type(exc).__name__
        print("WHALE_SMOKE_STOP " + summary["live"] + " error_type=" + type(exc).__name__, flush=True)
        if summary["live"] == "blocked_missing_CSQAQ_TOKEN":
            print("Configure repository Actions secret CSQAQ_TOKEN, then rerun this job.", flush=True)
        raise SystemExit(1)
    finally:
        whale.CSQAQClient._request = original_request
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        (output / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
                f.write("## Whale small-sample check\n\n")
                f.write("Offline: **" + summary["offline"] + "**\n\nLive: **" + summary["live"] + "**\n\n")
                f.write("CD_RECOVER only; isolated test, no production state.\n")


if __name__ == "__main__":
    run()
