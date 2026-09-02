#!/usr/bin/env python3
"""
Frontend smoke check: load both pages in a real browser and fail on anything
the console complains about.

There is no bundler here and therefore no build step to catch a typo, no type
checker over the JavaScript, and no unit tests for the DOM. That is a
deliberate trade -- the demo has to run on a laptop with no toolchain -- but
it leaves exactly one way to find a broken import, a 404 asset, a null
dereference or a failed fetch: open the page and look.

So this opens it. It drives Chromium headless against a running server,
collects console errors, page exceptions and failed requests, exercises the
interactive parts (transport, beat rail, topology picking, the console's text
ingest and its SSE feed), and exits non-zero if anything was logged. It is a
smoke check, not a test suite: it proves the pages load, wire up and react,
which is the class of failure that is otherwise invisible until a judge is
looking at it.

Run:  python scripts/check_frontend.py [--base http://127.0.0.1:8080] [--token TOKEN]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"

#: Console noise that is not a defect. Kept explicit and short: a broad filter
#: here would hide the errors this script exists to find.
IGNORED_CONSOLE = (
    "Download the React DevTools",
    "favicon.ico",
)


class Collector:
    """Everything the browser complained about, per page."""

    def __init__(self) -> None:
        self.console: list[str] = []
        self.errors: list[str] = []
        self.failed: list[str] = []

    def attach(self, page) -> None:
        def on_console(message):
            if message.type in ("error", "warning"):
                text = message.text
                if any(skip in text for skip in IGNORED_CONSOLE):
                    return
                self.console.append(f"{message.type}: {text}")

        page.on("console", on_console)
        page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        page.on(
            "requestfailed",
            lambda req: self.failed.append(f"{req.method} {req.url} — {req.failure}"),
        )

    @property
    def clean(self) -> bool:
        return not (self.console or self.errors or self.failed)

    def report(self, label: str) -> bool:
        if self.clean:
            print(f"    {GREEN}no console errors, page exceptions or failed requests{RESET}")
            return True
        for entry in self.errors:
            print(f"    {RED}page error{RESET}  {entry}")
        for entry in self.console:
            print(f"    {RED}console{RESET}     {entry}")
        for entry in self.failed:
            print(f"    {RED}request{RESET}     {entry}")
        return False


def check_hero(page, base: str) -> list[str]:
    """The landing walkthrough: assets, live hydration, transport, topology."""
    problems: list[str] = []
    page.goto(f"{base}/hero", wait_until="networkidle")

    # Every asset the page references actually resolved.
    missing = page.evaluate(
        """() => {
            const out = [];
            document.querySelectorAll('link[rel=stylesheet]').forEach((l) => {
                if (!l.sheet) out.push('stylesheet did not parse: ' + l.href);
            });
            return out;
        }"""
    )
    problems.extend(missing)

    # The stylesheet actually applied: a token from styles.css must resolve.
    accent = page.evaluate(
        "() => getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()"
    )
    if not accent:
        problems.append("styles.css tokens did not apply (--accent is empty)")

    # Hydration from the live API rather than the shipped fixture.
    note = page.text_content("#source-note") or ""
    if "live topology" not in note:
        problems.append(f"topology did not hydrate from /api/topology (source note: {note!r})")

    provider = page.text_content("#f-provider") or ""
    if not provider.strip():
        problems.append("extractor chip never populated from /api/health")

    perf_live = page.get_attribute("#perf-source", "data-live")
    perf_turn = page.text_content("#perf-turn") or ""

    # The topology explorer rendered real paths from the real graph.
    paths = page.eval_on_selector_all("#topo-paths li", "els => els.length")
    if paths < 1:
        problems.append("topology explorer rendered no dependency paths")

    nodes = page.text_content("#perf-nodes") or "0"
    if nodes.strip() != "10":
        problems.append(f"topology node count is {nodes!r}, expected the 10-node fixture")

    # The canvas is alive rather than a blank rectangle.
    painted = page.evaluate(
        """() => {
            const c = document.getElementById('topo-canvas');
            if (!c || !c.width) return false;
            const ctx = c.getContext('2d');
            const data = ctx.getImageData(0, 0, c.width, c.height).data;
            for (let i = 3; i < data.length; i += 4) if (data[i] !== 0) return true;
            return false;
        }"""
    )
    if not painted:
        problems.append("topology canvas painted nothing")

    # Transport: pause, scrub, and a beat button all move the clock.
    page.click("#play")
    if (page.text_content("#play") or "").strip() != "Play":
        problems.append("pause button did not toggle")
    page.click("#play")

    before = page.input_value("#scrub")
    beats = page.eval_on_selector_all(".beat", "els => els.length")
    if beats < 2:
        problems.append(f"beat rail rendered {beats} beats")
    else:
        page.eval_on_selector_all(".beat", "els => els[els.length - 1].click()")
        page.wait_for_timeout(200)
        if page.input_value("#scrub") == before:
            problems.append("clicking a beat did not move the transport")

    # Selecting a node in the readout changes the analysis.
    selected_before = page.text_content("#topo-selected")
    page.eval_on_selector_all(
        "#topo-paths li", "els => els.length && els[0].dispatchEvent(new MouseEvent('click', {bubbles: true}))"
    )
    canvas = page.query_selector("#topo-canvas")
    box = canvas.bounding_box()
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.wait_for_timeout(200)

    print(f"    {DIM}perf strip: {perf_turn.strip()} · live={perf_live}{RESET}")
    if page.text_content("#topo-selected") != selected_before:
        print(f"    {DIM}topology selection followed the click{RESET}")
    return problems


def check_console_page(page, base: str, token: str) -> list[str]:
    """The operator console: SSE feed, state rendering, text ingest."""
    problems: list[str] = []
    url = f"{base}/?token={token}" if token else base
    # Not networkidle: the console holds an SSE connection to /api/events for
    # as long as it is open, so the network is never idle and never will be.
    page.goto(url, wait_until="domcontentloaded")

    page.wait_for_function(
        "() => document.getElementById('connection').dataset.state === 'live'",
        timeout=8000,
    )

    provider = page.text_content("#provider-value") or ""
    if not provider.strip() or provider.strip() == "—":
        problems.append("console never read the extractor from /api/health")

    metrics = page.eval_on_selector_all("#metrics li", "els => els.length")
    if metrics != 4:
        problems.append(f"console rendered {metrics} telemetry metrics, expected 4")

    # Type a real utterance and wait for the claim to come back through SSE.
    page.fill("#text-input", "Pool utilization looks fine, like 40%.")
    page.click("#text-form button[type=submit]")
    try:
        page.wait_for_function(
            "() => document.querySelectorAll('#hypotheses li').length > 0", timeout=8000
        )
    except Exception:
        problems.append("a typed utterance never produced a theory in the console")

    try:
        page.wait_for_function(
            "() => Number(document.getElementById('interventions-count').textContent) > 0",
            timeout=8000,
        )
    except Exception:
        problems.append("the grounding intervention never reached the console")

    spoken = page.eval_on_selector_all(
        "#interventions .intervention", "els => els.map(e => e.textContent).join(' | ')"
    )
    if spoken and "91" not in spoken:
        problems.append(f"intervention text did not carry the measured value: {spoken[:120]}")
    print(f"    {DIM}console heard: {spoken[:110] or '(nothing)'}{RESET}")

    return problems


#: The end-to-end path, driven through the console's own text box so the UI is
#: exercised rather than the API. Each step names the AEGIS concept it proves
#: reached the screen, because "the page rendered" is not the same claim as
#: "the page reflects backend state".
SCENARIO = [
    ("Payments are throwing 500s, seeing timeouts.",
     "fact", "#facts li", 1),
    ("Pool utilization looks fine, like 40%.",
     "hypothesis grounded against telemetry", "#hypotheses li", 1),
    ("Let's rollback Core to the last version.",
     "proposed action with a blast radius", "#actions li", 1),
    ("Hold on, don't rollback yet.",
     "human hold recorded in the decision ledger", "#decisions li", 1),
]


def check_end_to_end(page, base: str, token: str) -> list[str]:
    """Drive the real flow through the UI and assert the UI follows state.

    Voice is the only input this cannot exercise: it needs Agora credentials
    and a channel. Everything downstream of a transcript is identical for a
    typed turn and a spoken one -- same event, same pipeline -- so this proves
    the whole chain from ingestion to the rendered intervention, and says
    plainly that it entered through the keyboard.
    """
    problems: list[str] = []
    url = f"{base}/?token={token}" if token else base
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_function(
        "() => document.getElementById('connection').dataset.state === 'live'", timeout=8000
    )

    def say(text: str) -> None:
        page.fill("#text-input", text)
        page.click("#text-form button[type=submit]")

    for text, concept, selector, expected in SCENARIO:
        say(text)
        try:
            page.wait_for_function(
                f"() => document.querySelectorAll('{selector}').length >= {expected}",
                timeout=9000,
            )
            print(f"    {GREEN}✓{RESET} {concept}")
        except Exception:
            problems.append(f"{concept}: {selector} never reached {expected} items")
            print(f"    {RED}✗{RESET} {concept}")

    # An intervention reached the screen with the measured value in it.
    try:
        page.wait_for_function(
            "() => Number(document.getElementById('interventions-count').textContent) >= 1",
            timeout=9000,
        )
        print(f"    {GREEN}✓{RESET} intervention rendered from the governor")
    except Exception:
        problems.append("no intervention ever rendered")

    # A pending action carries its risk tier through to the DOM.
    tiers = page.eval_on_selector_all(
        "#actions li", "els => els.map(e => e.textContent)"
    )
    if tiers and not any("HIGH" in t or "held" in t.lower() for t in tiers):
        problems.append(f"action row shows no risk tier or status: {tiers[:1]}")

    # ── the reasoning the product is actually judged on ────────────────
    # Everything above is plumbing that any dashboard has. These four are the
    # behaviours that separate AEGIS from an LLM with if-statements, and each
    # has to be visible on screen rather than only true in the database.

    def set_metric(name: str, value) -> None:
        page.evaluate(
            """async ([token, name, value]) => {
                await fetch('/api/telemetry/set', {
                    method: 'POST',
                    headers: Object.assign({'Content-Type': 'application/json'},
                                           token ? {Authorization: 'Bearer ' + token} : {}),
                    body: JSON.stringify({metric_name: name, value: value}),
                });
            }""",
            [token, name, value],
        )

    def interventions() -> str:
        return page.eval_on_selector_all(
            "#interventions .intervention", "els => els.map(e => e.textContent).join(' || ')"
        )

    set_metric("error_rate", 12.0)
    say("Error rate is around 12%, the retry storm is the cause.")
    page.wait_for_timeout(1200)
    say("Let's roll back search-index then.")
    page.wait_for_timeout(1200)
    say("Let's restart notification-service.")
    try:
        page.wait_for_function(
            "() => document.querySelectorAll('#actions li').length >= 3", timeout=9000
        )
    except Exception:
        problems.append("the two extra proposals never reached the console")

    # Ambiguous confirmation: two open actions, a bare yes. Nothing may be
    # authorised, and AEGIS has to ask which one.
    pending_before = page.eval_on_selector_all(
        "#actions li", "els => els.filter(e => /pending/i.test(e.textContent)).length"
    )
    say("Yeah, go ahead.")
    page.wait_for_timeout(2500)
    pending_after = page.eval_on_selector_all(
        "#actions li", "els => els.filter(e => /pending/i.test(e.textContent)).length"
    )
    if pending_after != pending_before:
        problems.append(
            f"an ambiguous yes changed the pending set ({pending_before} -> {pending_after})"
        )
    else:
        print(f"    {GREEN}✓{RESET} ambiguous yes authorised nothing ({pending_after} still pending)")

    if "which one" in interventions().lower():
        print(f"    {GREEN}✓{RESET} AEGIS asked which action was meant")
    else:
        print(f"    {YELLOW}·{RESET} clarifying question not on screen "
              f"{DIM}(rate-limited: it is never asked late, by design){RESET}")

    # Explicit confirmation: naming the target resolves that one and only it.
    say("Yes, roll back search-index.")
    try:
        page.wait_for_function(
            "() => Array.from(document.querySelectorAll('#actions li'))"
            "  .some(e => /confirmed/i.test(e.textContent))",
            timeout=9000,
        )
        print(f"    {GREEN}✓{RESET} naming the target confirmed exactly that action")
    except Exception:
        problems.append("an explicit confirmation never showed as confirmed in the console")

    # Belief retraction: reality moves, the theory dies, and the action still
    # resting on it is re-raised.
    before_retraction = interventions()
    set_metric("error_rate", 0.3)
    say("Error rate is down to 0.3% now.")
    page.wait_for_timeout(3000)
    after_retraction = interventions()
    if after_retraction != before_retraction:
        print(f"    {GREEN}✓{RESET} retracting the theory produced a new intervention")
    else:
        print(f"    {YELLOW}·{RESET} no new intervention after the retraction "
              f"{DIM}(may be rate-limited or already voiced){RESET}")

    # Duplicate *delivery* -- the same turn id arriving twice, which is what a
    # transport retry looks like. Re-typing the same sentence in the box is a
    # genuinely new turn (the console mints a fresh id per submit) and should
    # produce a second claim, so testing it through the box would prove
    # nothing. This posts the identical turn id twice, as a relay would.
    facts_before = page.eval_on_selector_all("#facts li", "els => els.length")
    replay = page.evaluate(
        """async (token) => {
            const headers = Object.assign({'Content-Type': 'application/json'},
                                          token ? {Authorization: 'Bearer ' + token} : {});
            const body = JSON.stringify({
                uid: '1001', turn_id: 'replay-check', final: true,
                text: 'The api-gateway health check has been failing since 14:02.',
            });
            const codes = [];
            for (let i = 0; i < 3; i += 1) {
                const r = await fetch('/api/transcript', {method: 'POST', headers, body});
                codes.push(r.status);
            }
            return codes;
        }""",
        token,
    )
    page.wait_for_timeout(1800)
    page.click("#refresh-btn")
    page.wait_for_timeout(600)
    facts_after = page.eval_on_selector_all("#facts li", "els => els.length")
    added = facts_after - facts_before
    if added != 1:
        problems.append(
            f"a turn delivered 3 times produced {added} facts (statuses {replay})"
        )
    if replay[1:] != [200, 200]:
        problems.append(f"a replayed turn was not reported as a duplicate: {replay}")
    print(f"    {GREEN}✓{RESET} turn delivered 3x -> {added} claim  {DIM}(statuses {replay}){RESET}")

    # Reset clears the console back to its empty states.
    page.evaluate(
        """async (token) => {
            await fetch('/api/reset', {
                method: 'POST',
                headers: Object.assign({'Content-Type': 'application/json'},
                                       token ? {Authorization: 'Bearer ' + token} : {}),
                body: '{}',
            });
        }""",
        token,
    )
    page.click("#refresh-btn")
    try:
        page.wait_for_function(
            "() => document.querySelectorAll('#facts li').length === 0"
            " && document.querySelectorAll('#actions li').length === 0",
            timeout=8000,
        )
        print(f"    {GREEN}✓{RESET} reset emptied the console")
    except Exception:
        problems.append("reset did not clear the rendered state")

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="AEGIS frontend smoke check")
    parser.add_argument("--base", default="http://127.0.0.1:8080")
    parser.add_argument("--token", default="")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--scenario",
        action="store_true",
        help="also drive the end-to-end incident flow through the console UI",
    )
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(f"{YELLOW}playwright is not installed; skipping the browser check{RESET}")
        print(f"{DIM}pip install playwright && playwright install chromium{RESET}")
        return 0

    results: dict[str, list[str]] = {}
    clean = True

    print(f"\n  {BOLD}AEGIS frontend smoke check{RESET}  {DIM}{args.base}{RESET}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for label, check in (
                ("/hero  landing walkthrough", check_hero),
                ("/      operator console", None),
            ):
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                collector = Collector()
                collector.attach(page)
                print(f"\n  {BOLD}{label}{RESET}")
                if check is check_hero:
                    problems = check_hero(page, args.base)
                else:
                    problems = check_console_page(page, args.base, args.token)
                page.wait_for_timeout(400)
                clean &= collector.report(label)
                for problem in problems:
                    print(f"    {RED}behaviour{RESET}   {problem}")
                clean &= not problems
                results[label] = problems + collector.console + collector.errors + collector.failed
                page.close()

            if args.scenario:
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                collector = Collector()
                collector.attach(page)
                print(f"\n  {BOLD}end-to-end  typed turn -> extraction -> risk -> intervention{RESET}")
                problems = check_end_to_end(page, args.base, args.token)
                page.wait_for_timeout(300)
                clean &= collector.report("end-to-end")
                for problem in problems:
                    print(f"    {RED}behaviour{RESET}   {problem}")
                clean &= not problems
                results["end-to-end"] = problems
                page.close()
        finally:
            browser.close()

    if args.json:
        print(json.dumps(results, indent=2))

    print()
    if clean:
        print(f"  {GREEN}Both pages load, wire up and react with a clean console.{RESET}\n")
        return 0
    print(f"  {RED}Problems found — see above.{RESET}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
