# MultiModelCouncil

MultiModelCouncil is a local Windows application for consulting OpenAI, Anthropic
and Google models through Microsoft Agent Framework and its original Developer UI
(DevUI). You supply the question and, optionally, your own reference documents.
It retains each participant's ordinary output and the technical evidence needed
to investigate incomplete or failed runs.

Three real, context-capable configurations share one implementation:

- **Testing Council** uses the lower-cost configured models for integration work.
- **Conversation Council** uses the balanced configured models for discussion.
- **Frontier Council** uses the highest-capability configured models for demanding analysis.

Each execution has three sequential stages. In **INITIAL**, the providers respond
independently and concurrently. In **CRITIQUE**, each reviews two anonymous peer
proposals and returns one whole combined review. In **REVISION**, each revisits its
own proposal using the correctly mapped reviews. Participants run in parallel
within a stage; required stage barriers prevent dependent work from proceeding
without its inputs. The application does not force consensus or prove correctness.

Built on the framework's workflows, concurrent orchestration, provider adapters
and DevUI, this application adds its Council protocol, explicit routing checks,
frozen reference context, per-tier execution allowances, durable diagnostic and
transcript pairs, independent sibling collection after provider failures, and
operator-controlled selective revision recovery. Its small nonvisual DevUI
transport safeguard prevents automatic resubmission of an initial execution POST
after a lost reply. The original graph, events and controls remain intact.

For setup, operating details, model settings, recovery and troubleshooting, see
[SETUP.md](SETUP.md). Use Python 3.12 and the exact versions in
[requirements.txt](requirements.txt); the recorded reference runtime is Python
3.12.4 on Windows. Save your own provider keys and DevUI authentication token in
Windows User environment variables. Place optional Markdown/text documents in
context/DumpingGround, then use Launch-MAF.cmd and the authenticated
http://127.0.0.1:8080/ interface. Stop-MAF.bat uses the same ownership checks.

All three configurations make real API requests when Run is selected. Users
provide their own credentials and model access; executions and deliberate recovery
incur provider charges. Opening or selecting a Council does not dispatch requests.
There are no simulated profiles, automatic paid retries or fallback models.

Offline verification uses synthetic documents and mocked provider transports.
It covers routing, context, failure isolation, streaming evidence and recovery;
it does not establish account access, model quality, live refusal explanations,
billing or expensive long-form behavior. The full suite retains a deferred
upstream DevUI error/completed-envelope finding and exits 2 for that qualification
limit. Browser disconnect behavior and arbitrary full-run POST replay protection
remain limited. Verification in an existing environment does not prove a fresh
installation.

See [third-party notices and original-source license status](THIRD_PARTY_NOTICES.md).
