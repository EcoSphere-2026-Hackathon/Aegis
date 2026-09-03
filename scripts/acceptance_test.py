
import subprocess
from playwright.sync_api import sync_playwright
import requests

def run_playwright():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        
        page.goto("http://127.0.0.1:8080/command")
        page.wait_for_selector("#connection[data-state=\"live\"]", timeout=10000)
        print("[Playwright] Connected to SSE.")
        
        print("[Playwright] Running test scenario in background...")
        subprocess.run(["python", "scripts/test_pipeline_scenario.py"], check=True)
        
        page.wait_for_timeout(2000) 
        
        turns = page.eval_on_selector_all(".turn", "els => els.length")
        interventions = page.eval_on_selector_all(".intervention", "els => els.length")
        facts = page.eval_on_selector_all("#facts .item", "els => els.length")
        hypotheses = page.eval_on_selector_all("#hypotheses .item", "els => els.length")
        actions = page.eval_on_selector_all("#actions .item", "els => els.length")
        decisions = page.eval_on_selector_all("#decisions .item", "els => els.length")
        
        print(f"[Playwright] LIVE - Turns: {turns}, Interventions: {interventions}, Facts: {facts}, Hypotheses: {hypotheses}, Actions: {actions}, Decisions: {decisions}")
        
        print("[Playwright] Refreshing browser...")
        page.reload()
        page.wait_for_timeout(2000)
        
        turns_refreshed = page.eval_on_selector_all(".turn", "els => els.length")
        interventions_refreshed = page.eval_on_selector_all(".intervention", "els => els.length")
        facts_refreshed = page.eval_on_selector_all("#facts .item", "els => els.length")
        hypotheses_refreshed = page.eval_on_selector_all("#hypotheses .item", "els => els.length")
        actions_refreshed = page.eval_on_selector_all("#actions .item", "els => els.length")
        decisions_refreshed = page.eval_on_selector_all("#decisions .item", "els => els.length")
        
        print(f"[Playwright] REFRESHED - Turns: {turns_refreshed}, Interventions: {interventions_refreshed}, Facts: {facts_refreshed}, Hypotheses: {hypotheses_refreshed}, Actions: {actions_refreshed}, Decisions: {decisions_refreshed}")
        
        browser.close()

if __name__ == "__main__":
    try:
        requests.post("http://127.0.0.1:8080/api/reset")
        print("[Playwright] Backend state reset.")
    except Exception as e:
        print("Could not reset backend state.", e)
    run_playwright()

