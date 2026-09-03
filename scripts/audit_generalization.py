import os
import sys
import time
from datetime import datetime, timezone

from backend.common.config import load_config
from backend.common.models import TranscriptEvent
from backend.pipeline.factory import build_runtime

def print_state(runtime):
    view = runtime.store.incident_view(captured_at=datetime.now(timezone.utc))
    pending = runtime.store.pending_actions()
    
    print("  [STATE]")
    print(f"    Facts: {len(view.facts)}")
    for f in view.facts: print(f"      - {f.text}")
    print(f"    Hypotheses: {len(view.hypotheses)}")
    for h in view.hypotheses: print(f"      - {h.text}")
    print(f"    Pending Actions: {len(pending)}")
    for a in pending: print(f"      - {a.action_kind.value} on {a.target_ref}")
    print(f"    Decisions: {len(view.decisions)}")
    for d in view.decisions: print(f"      - {d.text} (stance: {d.stance.value})")

def main():
    config = load_config()
    runtime = build_runtime(config)

    runtime.pipeline._extraction._max_attempts = 1

    scenarios = [
        {
            "name": "Scenario 1 (Network/Edge)",
            "utterances": [
                "The BGP routing table on edge-router-sfo is corrupted.",
                "Could it be a bad route announcement from the ISP?",
                "I propose we drop the bgp-peer session.",
                "Wait, let me check the traffic logs first.",
                "Yeah the traffic is looping, proceed with dropping the session."
            ]
        },
        {
            "name": "Scenario 2 (Data Pipeline/AI)",
            "utterances": [
                "The weaviate-cluster is throwing timeout errors.",
                "It looks like the embedding-model nodes are bottlenecked.",
                "Let's scale up the embedding-model nodes.",
                "No, don't do that, it'll blow the budget.",
                "Okay, we will hold off on scaling."
            ]
        },
        {
            "name": "Scenario 3 (Storage/Blob)",
            "utterances": [
                "Users are getting permission denied on the assets-bucket.",
                "I bet the recent iam-policy script messed it up.",
                "Let's rollback the iam-policy.",
                "Yes, do the rollback immediately."
            ]
        },
        {
            "name": "Scenario 4 (Queue/Worker)",
            "utterances": [
                "The ffmpeg-worker queue is backed up.",
                "Are the gpu-nodes running out of memory?",
                "I think we should restart the ffmpeg-worker.",
                "Are we sure that's safe?",
                "Yes, go ahead and restart it."
            ]
        },
        {
            "name": "Scenario 5 (Cache/Memcached)",
            "utterances": [
                "The auth-token-cache is seeing a 90 percent miss rate.",
                "It might be a cache invalidation storm on the memcached-session-store.",
                "Let's flush the memcached-session-store completely.",
                "Hold on, flushing will cause a stampede.",
                "Right, let's not flush it then."
            ]
        },
        {
            "name": "Scenario 6 (Unrelated Utterance Test)",
            "utterances": [
                "Did anyone catch the game last night? I think the referee made a terrible call."
            ]
        }
    ]

    for sc in scenarios:
        print(f"\n=== {sc['name']} ===")
        runtime.reset()
        for idx, text in enumerate(sc["utterances"]):
            print(f"\nINPUT: {text}")
            event = TranscriptEvent(turn_id=f"{sc['name'][:2]}_{idx}", uid="u1", text=text, final=True)
            res = runtime.pipeline.handle_transcript(event)
            
            for c in res.claims:
                print(f"-> CLAIM TYPE: {c.type.name}")
                print(f"-> CLAIM TEXT: {c.text}")
                print(f"-> TARGET: {c.target_ref}")
                print(f"-> ACTION: {c.action_kind.name if c.action_kind else 'None'}")
                print(f"-> STANCE: {c.decision_stance.name if c.decision_stance else 'None'}")
                
            for d in res.decisions:
                print(f"  [INTERVENTION] {d.action.name}: {d.spoken_text}")
                
            print_state(runtime)
            sys.stdout.flush()
            time.sleep(16)

if __name__ == '__main__':
    main()
