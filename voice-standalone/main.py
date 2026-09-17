import _process_guard
_process_guard.ensure_libgomp_preloaded()

import os
import tempfile

from _compound_splitter import split_compound_utterance
import config
import dispatch
import llm_intent
import skills  # noqa: F401 — skills/ package (replaces flat skills.py)
import stt
import wiki_fastpath
from audio_capture import record_until_enter
from skills import _session_state, semantic_match

try:
    import tts
except Exception as _tts_err:
    tts = None


def _speak(text: str) -> None:
    if tts is None or not text.strip():
        return
    try:
        tts.speak(text)
    except Exception as exc:
        print(f"[tts error] {exc}")


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
            sub_commands = split_compound_utterance(text)
            if len(sub_commands) > 1:
                outputs = []
                for sub_cmd in sub_commands:
                    name, args = skills.match_skill(sub_cmd)
                    if name is not None:
                        print(f"[layer0] {name}({args})")
                    else:
                        if layer1_available:
                            name, args = semantic_match.match(sub_cmd)
                        if name is not None:
                            print(f"[layer1] {name}({args})")
                        else:
                            wiki_hit = wiki_fastpath.lookup(sub_cmd)
                            if wiki_hit is not None:
                                question, answer, url = wiki_hit
                                name, args = "answer_question", {"question": question, "answer": answer, "url": url}
                                print(f"[wiki] {name}({args})")
                            elif config.LLM_FALLBACK_ENABLED:
                                name, args = llm_intent.resolve_intent(sub_cmd)
                                if name is None:
                                    print(f"No matching action for '{sub_cmd}'.")
                                    continue
                                print(f"[llm] {name}({args})")
                            else:
                                print(f"No matching action for '{sub_cmd}'.")
                                continue

                    if _session_state.is_asleep() and name != "wake_back_up":
                        print("[asleep] (ignoring — say 'wake up' to resume)")
                        continue

                    result = dispatch.execute_skill(
                        name, args, text=sub_cmd,
                        on_review=lambda name, args: print(f"[REVIEW] about to run: {name}({args})"),
                    )

                    if result["outcome"] == "needs_confirmation":
                        tier, pause = result["tier"], result["pause"]
                        print(f"[{tier}] {pause['preview']}")
                        for warning in pause["warnings"]:
                            print(f"  ⚠ machine-scan: {warning}")
                        if pause["editable"]:
                            edit = input("Press Enter to run as-is, type a replacement, or 'n' to cancel: ")
                            sent = "" if edit.strip().lower() == "n" else (edit if edit else None)
                        else:
                            confirm = input("Run this? [Y/n]: ")
                            sent = "" if confirm.strip().lower() == "n" else None
                        result = dispatch.finish_confirm(result["generator"], sent, name=name, args=args, text=sub_cmd, tier=tier)
                        if result["outcome"] == "cancelled":
                            print("[cancelled]")
                            continue

                    out = result["output"].strip() or f"Ran: {name}"
                    outputs.append(out)

                if outputs:
                    consolidated = "; ".join(outputs)
                    print(consolidated)
                    _speak(consolidated)
                continue

            name, args = skills.match_skill(text)
            if name is not None:
                print(f"[layer0] {name}({args})")
            else:
                if layer1_available:
                    name, args = semantic_match.match(text)
                if name is not None:
                    print(f"[layer1] {name}({args})")
                else:
                    wiki_hit = wiki_fastpath.lookup(text)
                    if wiki_hit is not None:
                        question, answer, url = wiki_hit
                        name, args = "answer_question", {"question": question, "answer": answer, "url": url}
                        print(f"[wiki] {name}({args})")
                    elif config.LLM_FALLBACK_ENABLED:
                        name, args = llm_intent.resolve_intent(text)
                        if name is None:
                            print("No matching action.")
                            continue
                        print(f"[llm] {name}({args})")
                    else:
                        print("No matching action.")
                        continue

            # Asleep gates EXECUTION, not matching — we still always try to
            # recognize "wake up" specifically; everything else is ignored
            # while asleep rather than silently acting on it.
            if _session_state.is_asleep() and name != "wake_back_up":
                print("[asleep] (ignoring — say 'wake up' to resume)")
                continue

            result = dispatch.execute_skill(
                name, args, text=text,
                on_review=lambda name, args: print(f"[REVIEW] about to run: {name}({args})"),
            )

            if result["outcome"] == "needs_confirmation":
                tier, pause = result["tier"], result["pause"]
                print(f"[{tier}] {pause['preview']}")
                for warning in pause["warnings"]:
                    print(f"  ⚠ machine-scan: {warning}")
                if pause["editable"]:
                    edit = input("Press Enter to run as-is, type a replacement, or 'n' to cancel: ")
                    sent = "" if edit.strip().lower() == "n" else (edit if edit else None)
                else:
                    confirm = input("Run this? [Y/n]: ")
                    sent = "" if confirm.strip().lower() == "n" else None
                result = dispatch.finish_confirm(result["generator"], sent, name=name, args=args, text=text, tier=tier)
                if result["outcome"] == "cancelled":
                    print("[cancelled]")
                    continue

            output = result["output"]
            if output:
                print(output, end="")
                _speak(output)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"[error] Could not complete that command: {exc}")
            continue


if __name__ == "__main__":
    main()
