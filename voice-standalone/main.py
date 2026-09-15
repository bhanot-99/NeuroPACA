import os
import tempfile

import actions
import llm_intent
import skills  # noqa: F401 — skills/ package (replaces flat skills.py)
import stt
from audio_capture import record_until_enter
from skills import semantic_match


def main() -> None:
    # Warm up Layer 1 once, upfront — otherwise the ~1.3s embedding-model
    # load would hit as a surprise mid-conversation, the first time Layer 0
    # misses a match, instead of a known, one-time startup cost.
    print("Loading local semantic matcher...")
    semantic_match._ensure_index_built()

    print("Voice commander (push-to-talk). Ctrl+C to quit.")
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
                name, args = semantic_match.match(text)
                if name is not None:
                    print(f"[layer1] {name}({args})")
                else:
                    name, args = llm_intent.resolve_intent(text)
                    if name is None:
                        print("No matching action.")
                        continue
                    print(f"[llm] {name}({args})")

            actions.DISPATCH[name](args)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"[error] Could not complete that command: {exc}")
            continue


if __name__ == "__main__":
    main()
