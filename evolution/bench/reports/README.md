# TITAN-Bench reports (Vol 10 §6, TITAN-EVO-017 / ADR-EVO-02)

- **`champion.json`** — the committed baseline: the current champion config's bench
  scores. `scripts/check_bench.py` compares a challenger against this baseline
  (non-inferiority per `../gates.yaml`). Refresh it whenever the champion changes.
- **`challenger.json`** — *transient, per-EP*. The EP author runs `titan bench run` on
  the challenger config and attaches the report here on the EP branch. CI
  (`make check-bench`) blocks the merge until it is present and passes every track. It
  is **not** carried on `main` — an EP branch adds it, and it is squashed away on merge
  (the challenger becomes the new champion, so `champion.json` is refreshed instead).
