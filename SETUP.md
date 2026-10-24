# Setup and operations

## Install the pinned environment

The reference platform is Windows with CPython 3.12.4. PowerShell, the existing
Windows networking/process cmdlets, and Brave in a standard installation location
are used by the launcher. Node 23.10.0 is only needed for the offline JavaScript
transport test. No frontend build or npm installation is needed.

From this checkout, with Python 3.12 already installed:

~~~powershell
py -3.12 -m venv runtime/python/.venv
.\runtime\python\.venv\Scripts\python.exe -m pip install --pre -r requirements.txt
~~~

These are setup instructions, not a claim that a fresh installation was tested.
configuration/baseline-environment.txt records the reference package inventory;
requirements.txt pins the runtime dependency closure. Do not substitute a newer
DevUI bundle without reviewing its hash-checked transport integration.

Supply your own DEVUI_AUTH_TOKEN, OPENAI_API_KEY, ANTHROPIC_API_KEY and
GEMINI_API_KEY as Windows **User** environment variables. Use Windows' environment
variable editor; never commit actual values. The launcher needs all four and
refreshes only these variables in its child process. No model availability or
billing access is established by startup.

An empty context/DumpingGround is supported. Add only your own .md, .txt or
supported ZIP text sources locally. These inputs and generated logs/ are excluded
from release content. Do not place secrets in questions or documents: ordinary
provider replies may quote them and those replies are archived.

## Application behavior and operations

## Operation

Double-click `Launch-MAF.cmd` to start or reuse the verified backend, check its three
registrations with authentication, and open the existing Brave profile. DevUI binds
only to `http://127.0.0.1:8080`. Double-click `Stop-MAF.bat` to stop the verified MAF
backend. Do not stop it during a Council execution.

The launcher loads the saved Windows User `DEVUI_AUTH_TOKEN`, `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, and `GEMINI_API_KEY`. It does not generate credentials. A reused
backend retains the credentials and settings loaded when it started; saved changes
require a deliberate stop/start. Browser token persistence is DevUI's normal
behavior. Never put credentials in questions, context documents, or archives.

The only registered entries share one INITIAL → anonymous CRITIQUE → independent
REVISION implementation and the same read-only DumpingGround loader:

- **Testing Council**
- **Conversation Council**
- **Frontier Council**

The stable profile keys are testing, conversation and frontier. DevUI derives
entity IDs from each generic display name plus a startup-specific suffix; read
the current authenticated /v1/entities listing when using its API.

All three use real provider integrations. Opening/selecting an entry does not call
providers or read context. Clicking Run starts a real, billable nine-request Council;
there is no activation flag or fallback execution mode. No automatic retries, model
substitutions, tools, web verification, forced consensus, cooldown or one-run limit
are added. Actual cost remains unknown when billing data is unavailable.

## Models, reasoning, and execution allowances

Edit the existing `configuration/council/profiles.json`. Selected model IDs and
reasoning settings are preserved; registration names, explicit token caps, transport
streaming and per-tier execution limits are recorded there. Provider errors are
reported without selecting another model. Account access is not checked at startup.

| Council | OpenAI: model / reasoning / output cap | Anthropic: model / effort / output cap | Google: model / thinking / output cap |
| --- | --- | --- | --- |
| Testing | `gpt-6-luna` / `none` / 32,768 | `claude-haiku-5-5` / `low` / 32,768 | `gemini-3.1-flash-lite` / `MINIMAL` / 32,768 |
| Conversation | `gpt-6.1-sol` / `medium` / 65,536 | `claude-sonnet-5-5` / `medium` / 65,536 | `gemini-3.8-flash` / `MEDIUM` / 65,536 |
| Frontier | `gpt-6-astra` / `max`, `mode=pro` / 128,000 | `claude-fable-5-1` / `max` / 128,000 | `gemini-4-argon` / `HIGH` / 65,536 |

| Council | Request / stage / Council deadline (seconds) | Question bytes | Response bytes | Assembled-request text bytes |
| --- | --- | --- | --- | --- |
| Testing | 600 / 610 / 1,860 | 4,096 | 262,144 | 5,242,880 |
| Conversation | 1,800 / 1,810 / 5,460 | 4,096 | 524,288 | 10,485,760 |
| Frontier | 3,600 / 3,610 / 10,860 | 4,096 | 1,024,000 | 20,480,000 |

These are editable local allowances, not provider context-window or completion-time
guarantees. This build preserves the current profiles, including the previously
expanded deadlines. Higher-tier
byte limits grow with the output allowances; byte limits are independent guards,
not exact token conversions. Validation rejects conflicting response allowances and
requires stage > request and Council >= three stages. Requests are checked before
dispatch; a later feedback envelope can exceed the request limit even if INITIAL
fits. Boundary failures report actual/configured UTF-8 bytes without clipping.

The installed OpenAI integration maps `max_tokens` to Responses `max_output_tokens`
and passes the reasoning object, including Frontier's `mode=pro`. That cap includes
reasoning and visible output; pro mode can use more tokens and take longer. See
[OpenAI reasoning and pro mode](https://developers.openai.com/api/docs/guides/reasoning).
Anthropic caps also include thinking, which is on by default for these configured
models; the selected effort is forwarded unchanged. Conversation and Frontier use
one streamed Anthropic request, aggregated into a complete framework response, to
support long reasoning/output without depending on an idle non-streaming request.
Testing retains its working non-streaming path. See [Anthropic effort](https://platform.claude.com/docs/en/build-with-claude/effort)
and [long requests](https://platform.claude.com/docs/en/api/errors#long-requests).
Gemini's cap includes thinking and the reply. Frontier uses the documented 65,536
single-request allowance, with automatic continuation disabled; longer decode is not
silently introduced. See [Gemini 4 Argon GenerateContent guidance](https://ai.google.dev/gemini-api/docs/generate-content/latest-model).
A provider truncation or timeout still fails the Council. Real access, latency and
quality require provider responses; offline fixtures do not establish them.

## Questions and reference context

Use the working Simple Text input. The installed direct application programming
interface (API) converts workflow input to a string; arbitrary JSON objects and
additional `documents` fields are not a supported document-upload interface.

All three Councils read `context/DumpingGround` at Run time, using the
same shared loader. Registration, listing and selection do not read sources.
The loader supports `.md`, `.txt`, and readable Markdown/text members of ZIP
archives, without extraction.
UTF-8 and byte-order-marked UTF-16/UTF-32 text are supported. Filesystem links and
archive links are not followed. Unsupported/unreadable sources are listed as
omitted, while readable material continues. The frozen reference includes that
omission inventory as source metadata, not higher-priority instructions.

Preparation is synchronous and sequential, with elapsed time recorded. The bundle
is frozen after preparation and reused throughout that execution. It is not an
atomic snapshot of a folder changing externally during preparation. A later Run
reads the sources again. No source-document snapshots are saved.
Async deadlines cannot preempt synchronous preparation; the initial live Council
budget is established after that preparation.

Source bodies remain private to execution and the small frozen-text recovery cache;
the per-execution ContextBundle object is released on completion/failure.
Only identities, hashes, byte counts and omissions enter coordinator/checkpoint
state and context archive sections. Every provider request in all three stages
receives that run's identical frozen reference. Dispatch evidence does not prove
semantic understanding or acknowledgment of the documents.

## Responses, routing, and evidence

Ordinary SDK-exposed response text is retained without prose stripping, newline
rewriting, heading parsing, or provider-keyword rejection. Hidden reasoning and raw
provider payloads are excluded. The SDK's combined text can insert separators
between text blocks; the transcript presents ordered ordinary blocks with readable
separators. Neither representation claims preservation of raw HTTP bytes.

Each reviewer returns one combined, unsegmented critique about its two anonymous
peer proposals. Coordinator-owned references route that complete review once to
each of those two authors. Each recipient receives the local reviewed-label map
and identification of its own proposal. This does not extract two independent
criticisms or prove that the model acknowledged the feedback. Coordinator metadata
is anonymous; model prose can reveal identity. Identical prose remains separately
attributed to each participant.

REVISION retains the whole response. An explicit, unambiguous `Position: revise`
or `Position: preserve` line can supply optional metadata; absent/conflicting
declarations mean `unspecified`. The application does not infer meaning or force
consensus. Refusals, truncation, missing content, tool calls, corrupted routing,
authentication/destination violations, cancellation, and expired deadlines still
fail. Native concurrent orchestration records observed order without requiring
every live participant to start before the fastest one finishes.

Each run uses `logs/<run-id>-Logs/<run-id>-Transcript.md` and the matching
`<run-id>-Diagnostics.jsonl`. Ordinary response bodies are written progressively;
context provenance is filenames, sizes, hashes, ZIP/member identities, and omissions
only. A provider may quote context in its ordinary response; such quoted response
text is retained. Request attempts, returned responses, stage acceptance, cleanup
warnings, and restored/new work are distinct. Model-authored Markdown headings are
not recorder status; diagnostic events and the recorder's final Run Outcome are
authoritative. Two-file appends are not atomic. Missing completion markers or an
archive warning mean the archive is incomplete, even if the Council result was
accepted. Ordinary recorder/cleanup errors do not replace the actual request outcome.

## Deferred limitations and offline verification

- Upstream DevUI can emit an error followed by a completed response envelope. A
  completed envelope alone is not evidence of an accepted Council result.
- Browser-refresh/disconnect cancellation is not fully qualified. An isolated
  provider failure now leaves independent siblings running; required stage barriers
  still block dependent work when a response is missing.
- Deliberately replaying a full-run POST can execute again and incur duplicate
  billing. The maintained frontend prevents automatic initial-POST resubmission
  after a lost reply; known-response reconnects use GET. Revision recovery has
  per-action replay protection. There is no question deduplication or cooldown.
- Concurrency protection is per workflow instance, not a global Council lock.
  Native pending-message/checkpoint behavior has its own limitations.
- Deliberate native checkpoint continuation is in-memory only. It grants the
  remaining work a fresh time budget, retains prior dispatch accounting, and
  requires matching context sources/hashes. It is not durable restart recovery,
  automatic resumption, or browser reconnection.

Run from the project directory using the existing interpreter:

```powershell
.\runtime\python\.venv\Scripts\python.exe -B -u -X utf8 .\tests\offline\council_workflow_smoke.py --registrations-only
.\runtime\python\.venv\Scripts\python.exe -B -u -X utf8 .\tests\offline\council_workflow_smoke.py --hardening-only
.\runtime\python\.venv\Scripts\python.exe -B -u -X utf8 .\tests\offline\council_workflow_smoke.py
.\runtime\python\.venv\Scripts\python.exe -B -u -X utf8 .\tests\offline\provider_probe.py --self-test
.\runtime\python\.venv\Scripts\python.exe -B -u -X utf8 .\tests\offline\council_browser.py
```

These use synthetic credentials and fake provider transports, with sockets and DNS
forbidden. `--registrations-only` runs bounded registration checks and exits **0**
on success: exactly three registrations, actual launcher readiness/negative cases,
all three tiers through installed SDK request builders, nine requests/run, exact
model/reasoning/token/HTTP-timeout settings, larger response headroom, one frozen
context per run, next-run reload, metadata-only archives, bundle release, and genuine
provider/stream failures without fallback. Owned source/archive fixtures live under
`tests/offline` and are removed on exit. No test reads the real DumpingGround or
starts a backend.

`--hardening-only` adds the provider-failure/stream matrix, independent sibling
collection, durable selective recovery, authenticated application routes, actual
served duplicate-POST transport code in a Node terminal harness, interruption,
redaction, recorder failures and bounded storage/fragmentation checks. Tests use
synthetic fixtures, including exact legacy-framing reconstruction and rejection
checks, and do not require historical archives or Git objects. They
exit **0** on success. Node 23.10.0 is the reference tool for the offline transport
test; no npm packages or frontend build are required.

The full Council suite includes both groups and the retained routing, presentation,
archiving, timing, checkpoint and failure regressions. It separates targeted success
from the known upstream DevUI error/completed-envelope finding and exits **2** while
qualification remains deferred; a new assertion/error exits **1**. The provider
self-test exits **0** on success. No live provider test is part of these commands.
All fixture documents and archives are synthetic, created under tests/offline
and removed afterward. The tests import this checkout's application, not another
installation. `frontier_recording.py` additionally checks HTTP-200 Anthropic
refusals with zero reported output tokens, unexplained refusals with nonzero token
usage but no ordinary text, transport interruption, missing terminal events and
HTTP-200 stream errors. A substantial successful streamed revision checks exact
ordinary-text bytes, provider identifiers, final usage and reported thinking-token
counters while excluding hidden thinking and signatures. Tests read archives back
from disk and reconstruct whole proposals, routed critiques, message order and
original context identities without redispatch. A zero-token refusal is recorded
while two independent revision siblings remain active; both subsequently finish,
and the incomplete stage stays rejected. These are synthetic adapter-to-archive
checks, not live refusal or billing qualification. They use installed adapters with
mocked HTTP only:

~~~powershell
.\runtime\python\.venv\Scripts\python.exe -B -u -X utf8 .\tests\offline\frontier_recording.py
~~~

A clean installation has not been verified for this candidate. Pinned package
versions describe the intended environment; they are not a wheel/hash lock.
.gitignore excludes runtime, archives, context content, credentials and generated
files. Ignore rules do not sanitize previously tracked files or arbitrary copies.

## Human-controlled load and interface checks

1. Let active work finish. Run the existing `Stop-MAF.bat`, then `Launch-MAF.cmd`.
   The launcher retains authentication, exact three-Council registration checks,
   loopback restrictions and process ownership verification. It initializes the original DevUI
   and its nonvisual transport safeguard. A reused process retains its old
   code/settings; the normal readiness check does not prove source freshness.
2. Open `http://127.0.0.1:8080/` in the existing Brave profile after restarting,
   and reload the old tab if it was already open. Use normal DevUI authentication
   if prompted. Select each Council without running it. The original INITIAL →
   CRITIQUE → REVISION graph, colors, timeline, Events and run controls are used.
   There is no provider grid or **Retry Revisions** menu item.
3. Read-only authenticated `/v1/council/installation` reports frozen startup
   profiles and loaded source/profile/template identities. Compare its loaded
   workflow source digest with `Get-FileHash .\application\council\workflow.py`.
   The latest run's normal archive records the same identities and its effective
   settings. The original DevUI does not display the enhanced diagnostics in a
   replacement provider panel.

`council_browser.py` uses the already-installed Brave headlessly, an isolated
scratch profile and an ephemeral loopback fixture server, never port 8080 or the
Human browser profile. It renders the full installed React application, selects
all three workflows, checks the original graph in idle/running/completed states,
verifies native stage colors, output/events/Stop/Run Again controls, and confirms
that the retired panel/assets/menu item are absent. Its SSE executions are synthetic;
provider dispatch and real context loading are forbidden. Scratch data is removed
on exit. No browser framework or dependency installation is required. This is an
actual rendering check, not a paid model test or a check of the Human's active tab.

## Recorder, independent collection and revision recovery

The existing matched Transcript/Diagnostics pair now carries schema-2 machine
evidence: question/profile once, module source hashes captured at import, the
actually compiled workflow code fingerprint, the nonvisual frontend-transport identity, package versions,
common system templates once, context identities, native and normalized finish
metadata, selected HTTP/request IDs, sanitized causes/stack locations, ordinary
text and individual acceptance. No request/response dumps or hidden reasoning
are collected. Provider-native usage snapshots retain cache/reasoning counters;
stream cumulative counts replace earlier counts rather than being added. Usage,
unknown actual billing and any Human-reported cost remain distinct.

OpenAI and Gemini retain non-streaming mode; Testing Anthropic retains its
non-streaming path. Conversation/Frontier Anthropic use their existing stream.
Non-streaming clients cannot preserve ordinary text they never exposed. Streamed
ordinary text is saved when first observed, then in batches of 4,096 characters,
or at least 256 characters after a second of activity, and at termination. Each
meaningful append flushes and fsyncs its file. No idle timer/heartbeat is archived.
A hard kill can lose the buffered tail; an absent/truncated final record remains
interrupted/unknown. The two files are not an atomic transaction. A failed
destination is disabled, with warnings through surviving archive/UI/terminal
channels; provider success/failure and archive completeness stay separate.

An ordinary provider refusal, request error, request timeout or response-validation
failure is collected by native MAF orchestration while independent siblings finish
under their existing deadlines. Usable responses are accepted individually. Missing
required results still reject the stage and block dependent work. Stop,
stage/run deadlines and shared destination/accounting integrity failures cancel
pending local work and join it. Local cancellation does not establish the provider's
remote final outcome or charge. The SDK retry settings remain zero for OpenAI and
Anthropic, one total Google attempt, with continuation/function looping disabled.

The original DevUI presents native workflow events and stage status. Recorder-owned
`provider_accepted`, `stage_accepted`, `run_completed` and `run_failed` evidence,
not an outer DevUI `response.completed` envelope, determines Council acceptance.
The enhanced diagnostics and complete ordinary transcripts remain in the normal
archive pair; no new graphical view is supplied.

Operator-controlled selective revision recovery remains a backend facility. It
requires accepted INITIAL and CRITIQUE and reconstructible original inputs. It
reuses all accepted responses by reference and sends only the one, two or three
missing revisions. It never feeds peer revisions to a participant or reruns either
earlier stage. Normal DevUI Run Again is a new full Council and reloads current
context. Recovery uses the original question, templates, routing, model/reasoning/
token/stream settings and deadline allowances, with a fresh recorded budget. It
is new, potentially billable inference and can fail again; it is never automatic.

After restart, reconstruction reads recorder-owned acceptance/routing and ordinary
body records. Compatible single-block records can be reconstructed by
exact diagnostic byte counts and recorder framing; model headings have no control
meaning. Legacy multi-block records without exact boundaries, incomplete original
acceptance evidence and corrupt routing are reported unavailable, never guessed.
Native in-memory checkpoint continuation remains a separate facility.

Original context is reused from a small three-entry in-process frozen-text cache
when available. Otherwise the shared loader reads only originally referenced
files/members, verifies their raw hashes/sizes and reproduces the original envelope.
New/unrelated files are excluded. ZIP member index/name/content must match; an
unrelated changed container member need not invalidate the referenced contents.
ZIP container hashes in that envelope describe the original container. Missing or
changed required content is identified before dispatch; hashes cannot recreate it.
There are no durable context/prompt snapshots. Current profile changes do not
replace recoverable original settings.

Each deliberate recovery has one new normal two-file archive linked to its source
and reused response identities. The original failed archive is unchanged. Later
accepted revisions are followed through that lineage so selecting an older source
cannot repeat them. Action IDs bind to source fingerprints and are recorded for
replay protection across restart; active actions for the same source cannot be
duplicated. Refresh/status GETs do not dispatch. There is no detached job service:
the existing DevUI stream cancels on observed disconnect, and the recovery stream
is also attached to its HTTP request. Only observable Human Stop/deadline causes
are identified precisely; otherwise request cancellation/shutdown remains unknown.

## Recovery API and duplicate-request protection

The backend adapter in `application/council/devui.py` retains cancellation-cause
tracking, installation metadata, recovery replay/active-action guards and these
Bearer-authenticated endpoints:

| Method and route | Supported behavior |
| --- | --- |
| `GET /v1/council/runs/{run_id}/recovery` | Read-only eligibility, latest recoverable source, missing providers/new billable attempt count, fingerprint, reason and local recovery status. It does not load context or call providers. |
| `POST /v1/council/runs/{run_id}/retry-revisions` | Explicit operator recovery. JSON contains exactly `action_id` (a fresh UUID) and the reviewed source `fingerprint`. Returns SSE run identity/progress, or JSON for a repeated action. |
| `POST /v1/council/runs/{new_run_id}/stop-recovery` | Requests cancellation of that locally active recovery task. Remote completion/billing remains unknown. |

For a read-only eligibility check from PowerShell:

```powershell
$mafHeaders = @{Authorization = 'Bearer ' + [Environment]::GetEnvironmentVariable('DEVUI_AUTH_TOKEN', 'User')}
$mafSource = '<run-id>' # Replace with the original failed run identity.
$mafState = Invoke-RestMethod -Method Get -Headers $mafHeaders -Uri ('http://127.0.0.1:8080/v1/council/runs/' + $mafSource + '/recovery')
$mafState
```

There is no recovery button, replacement GUI or installed recovery CLI. An operator
may use an existing HTTP client to submit the explicit POST after reviewing
eligibility and spending. Keep its UUID and fingerprint: repeating the identical
action retrieves its recorded identity instead of dispatching again, including
after restart. A deliberate subsequent recovery needs fresh readiness and a new
UUID. HTTP clients must consume the SSE connection through completion; a buffering
client may show progress only when finished. The stream is request-bound, not a
detached job. Disconnects or client timeouts can cancel local work, without proving
remote cancellation. The original archive and each new linked archive remain the
authority for outcomes and text.

The installed DevUI would automatically resend an initial execution POST after a
lost response if it had not learned a response ID. `devui_transport.py` retains
only the small nonvisual guard against that resend. It checks installed bundle
SHA-256 `1bb5d50943f3ba82dc17abb9aa615726ba8ffc746a18fd58e8b273f78fffed9f`
and its single retry anchor before serving. Removing that guard from the served
bundle yields the installed original bundle byte-for-byte: graph, controls, menus,
labels, HTML and CSS are unchanged. No site-packages files are modified.

A lost initial reply now surfaces an outcome-unknown error after one POST. With a
known response ID, read-only GET reconnects remain available. Deliberate new full
Runs, including the same question, remain allowed. This is not server-side
idempotency for arbitrary full-run POSTs: another client or an explicit replay can
still execute again. Revision recovery has separate durable action-ID replay
protection. No question cooldown, automatic retry, fallback model or new framework
is introduced. An upstream frontend change requires review of this narrow guard.

## Troubleshooting and qualification boundaries

- Missing credential or Brave errors stop launch before opening the application.
  Configure the documented User variables or use Launch-MAF.cmd --no-browser.
- A reused backend retains its startup code/settings. After all work has finished,
  use the normal Stop/Launch sequence when you intend to load changed source.
- Port 8080 belonging to another checkout or application is refused. The launcher
  never kills by name, walks a process tree or takes over another installation.
- A changed DevUI bundle hash fails closed. Restore the pinned dependency rather
  than disabling the duplicate-POST safeguard.
- Check the matching diagnostics and final Run Outcome for acceptance. An upstream
  response.completed envelope can follow an error and does not establish success.
- Output token caps, request/stage/run deadlines and input/output byte guards are
  independent limits. Provider refusals, missing terminal events and partial text
  are evidence, not accepted results. Usage is provider-reported; actual billing
  remains unknown unless separately established.
- Live access, future refusal explanations, remote cancellation and expensive
  inference remain unqualified by these offline fixtures. Providers may expose no
  private reason/category, final usage or final billing after an interruption.
