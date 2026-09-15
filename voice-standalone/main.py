import contextlib
import io
import os
import tempfile
import time

import actions
import config
import llm_intent
import skills  # noqa: F401 — skills/ package (replaces flat skills.py)
import stt
from audio_capture import record_until_enter
from confirm_loop import confirm_and_run
from skills import _audit, _session_state, _tiers, semantic_match


def main() -> None:
    # Warm up Layer 1 once, upfront — otherwise the ~1.3s embedding-model
    # load would hit as a surprise mid-conversation, the first time Layer 0
    # misses a match, instead of a known, one-time startup cost. If it fails
    # (no network for the first-ever model download, a corrupted cache),
    # the assistant must still start — it just runs Layer 0 + LLM fallback
    # only for this session, same principle as every other try/except here.
    print("Loading local semantic matcher...")
    layer1_available = True
    try:
        semantic_match._ensure_index_built()
    except Exception as exc:
        layer1_available = False
        print(f"[warning] Layer 1 (semantic match) unavailable this session: {exc}")
        print("[warning] Continuing with Layer 0 + LLM fallback only.")

    print("Voice commander (push-to-talk) — ready. Ctrl+C to quit.")
    while True:
        try:
            input("\nPress Enter to start recording...")
        except (EOFError, KeyboardInterrupt):
            break

        print("Recording... press Enter to stop.")
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = tmp.name

        # A transient API error, a filtered/empty model response, or a bad
        # argument in any single executor must not kill the whole assistant —
        # log it and go back to listening instead.
        try:
            record_until_enter(wav_path)
            text = stt.transcribe(wav_path)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"[error] Could not transcribe that: {exc}")
            continue
        finally:
            if os.path.exists(wav_path):
                os.unlink(wav_path)

        print(f"Heard: {text!r}")
        if not text:
            continue

        try:
            name, args = skills.match_skill(text)
            if name is not None:
                print(f"[layer0] {name}({args})")
            else:
                if layer1_available:
                    name, args = semantic_match.match(text)
                if name is not None:
                    print(f"[layer1] {name}({args})")
                else:
                    name, args = llm_intent.resolve_intent(text)
                    if name is None:
                        print("No matching action.")
                        continue
                    print(f"[llm] {name}({args})")

            # Asleep gates EXECUTION, not matching — we still always try to
            # recognize "wake up" specifically; everything else is ignored
            # while asleep rather than silently acting on it.
            if _session_state.is_asleep() and name != "wake_back_up":
                print("[asleep] (ignoring — say 'wake up' to resume)")
                continue

            tier = _tiers.tier_of(name)

            if tier == "DANGEROUS" and config.SAFETY_TIERS_ENABLED:
                gen = confirm_and_run(name, args)
                pause = next(gen)
                print(f"[{tier}] {pause['preview']}")
                for warning in pause["warnings"]:
                    print(f"  ⚠ machine-scan: {warning}")
                if pause["editable"]:
                    edit = input("Press Enter to run as-is, type a replacement, or 'n' to cancel: ")
                    sent = "" if edit.strip().lower() == "n" else (edit if edit else None)
                else:
                    confirm = input("Run this? [Y/n]: ")
                    sent = "" if confirm.strip().lower() == "n" else None
                try:
                    gen.send(sent)
                    result = None
                except StopIteration as done:
                    result = done.value
                if result and result["cancelled"]:
                    print("[cancelled]")
                    _audit.record(text=text, skill_name=name, args=args, tier=tier, outcome="cancelled")
                    continue
                output = result["output"] if result else ""
                if output:
                    print(output, end="")
                    _session_state.set_last_response(output)
                _audit.record(text=text, skill_name=name, args=args, tier=tier, outcome="executed")
                continue

            if tier == "REVIEW" and config.SAFETY_TIERS_ENABLED:
                print(f"[{tier}] about to run: {name}({args})")
                time.sleep(2)

            # Capture what the executor prints so "repeat that" (J07) works
            # without rewriting every existing executor to return a string
            # instead of printing — captured text is still shown live, just
            # via this buffer instead of print() writing straight to stdout.
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                actions.DISPATCH[name](args)
            output = buffer.getvalue()
            if output:
                print(output, end="")
                _session_state.set_last_response(output)
            _audit.record(text=text, skill_name=name, args=args, tier=tier, outcome="executed")
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"[error] Could not complete that command: {exc}")
            continue


if __name__ == "__main__":
    main()
