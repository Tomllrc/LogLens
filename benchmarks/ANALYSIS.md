# Benchmark analysis: what failed, what was fixed, what was left alone

Companion to [RESULTS.md](RESULTS.md). Rule followed throughout: only **general** fixes
(rules that apply to any log), never rules keyed to a dataset or to its ground truth.
Drain's similarity threshold (0.4) was never changed.

## Baseline (end of Phase 1)

Average grouping accuracy (GA) **66.1%**, parsing accuracy (PA) **28.9%**. The worst
datasets were Proxifier (0.0), OpenStack (21.2), Android (29.2), HPC (36.9) and
Thunderbird (47.9).

## Round 1: why the worst datasets failed

Real lines from the failing groups (ground truth "Content" vs what LogLens had left in the
message before mining):

| Dataset | Line (header part in bold) | Cause |
|---|---|---|
| Proxifier | **chrome.exe \*64 -** www.baidu.com:80 open through proxy ... | `program [tag] - message` header not recognized; `*64` made otherwise identical lines differ |
| OpenStack | **25746 INFO nova.osapi_compute.wsgi.server [req-38101a0b... - - -]** 10.11.10.1 "GET ..." | `qualified.logger [request context] message` not recognized; the request id varied per line |
| Android | **1702  8671 D PowerManagerService:** ready=true,policy=3,... | pid/tid then a single-letter level (logcat/glog style) not recognized, so `Tag:` was not taken as the component |
| Thunderbird | ... **/apps/x86_64/.../gmetad[1682]:** data_thread() got not answer ... | syslog tags that are paths were not accepted |
| HPC | 134681 node-246 ... 1077804742 **1** normal / **1** running | `<NUM> normal` and `<NUM> running` share a placeholder, which Drain counted as similarity: 2-token events merged |
| HPC | ... 1077804742 1 **warning** | the level rule took the only word of the message as a level, leaving it empty |
| Proxifier | proxy.cse.cuhk.edu.hk:5070, www.baidu.com:80 | host names were not masked |

General fixes (each applies to any log):

1. Generic parser: `program [tag] - message` headers; `qualified.name [context]` after a
   level (one context group skipped); single-letter levels (V/D/I/W/E/F) only when
   immediately followed by a `Tag:`; syslog tags may be paths; a message is never empty.
2. Masking: a `<HOST>` placeholder (host names ending in a common TLD, or 3+ labels ending
   in a country code, so file names such as `main.go` are not hosts).
3. Mining: two equal typed placeholders are neutral in Drain's similarity (they say the
   values have the same kind, not that the events are the same); only literal tokens
   count as similar. A typed placeholder facing a different kind still counts against.

Result: GA **66.1 -> 76.2** (+10.1), PA 28.9 -> 33.1. Android 29.2 -> 73.4, HPC 36.9 ->
81.7, Thunderbird 47.9 -> 91.6, BGL 85.3 -> 95.7, OpenStack 21.2 -> 37.2, Proxifier 0.0
-> 2.5. No dataset got worse.

## Round 2: the next worst (Proxifier, OpenStack, Windows)

Cross-tabulating Proxifier's templates against the true event ids showed that 945 lines
of event E2 were in one template, but 9 more E2 lines were elsewhere: 8 in a cluster where
Drain had generalized `HTTPS`/`SOCKS5` (`<IP> open through proxy <HOST> <*>`), and one
because `360AP.exe` (a program name starting with a digit) was not recognized. GA only
credits a group when it holds every line of its event, so the whole 954-line event scored
zero.

General fixes:

1. Program names may start with a digit (`360AP.exe`).
2. Only the first bracket group after a logger name is header context: OpenStack's
   `[instance: 42]` belongs to the message (the ground truth keeps it too). Round 1 had
   been dropping it, which lost real message content.
3. Delimited files without a header now recognize every timestamp layout the generic
   parser knows (HealthApp's `20171223-22:15:29:606`).

Result: HealthApp timestamps 0% -> 100% found and correct; OpenStack PA 20.5 -> 24.8 but
GA 37.2 -> 30.9; average GA 76.2 -> 75.8. Fix 2 was kept although it lowers OpenStack's GA,
because dropping message content is a correctness bug regardless of the score.

## What remains, and why it was left alone

- **Drain generalizes near-identical events.** `"GET ..."` vs `"POST ..."` (OpenStack),
  `HTTPS` vs `SOCKS5` (Proxifier), `client WindowsUpdateAgent.` vs other clients (Windows)
  are 1 token apart in 5-10 tokens, above the 0.4 similarity threshold. Per-dataset
  thresholds would fix them; that is tuning to the benchmark.
- **Optional tokens.** `1190 bytes (1.16 KB) sent` vs `451 bytes sent` (Proxifier) have
  different token counts, which Drain never merges.
- **Ground-truth conventions.** Templates such as `blk_<*>` or `<*>:<*>` keep partial
  tokens; LogLens masks whole tokens (`<*>`, `<HOST>`). This lowers PA (HDFS: GA 99.8,
  PA 0.0) without affecting grouping.
- **Format-specific headers.** Windows CBS logs (`Info  CBS  message`) put an
  upper-case component without a colon after the level; telling it from the first word of
  a message needs a known format, not a general rule. Adding a YAML format for it is the
  right fix (Phase 3 makes that easy, including the AI learner).
