# Spikes

Time-boxed experiments that inform design decisions for Wax and HiveAR, published by The Open Accounts Receivable Collective Foundation, operating as The OpenAR Collective.

Nothing in this repository is the Wax framework or the HiveAR platform, and none of it is supported, maintained, or fit for production use. Each experiment answers one question that a design decision needs answered, records the result, and then stops. The decisions themselves live in [platform-design](https://github.com/OpenAR-Collective/platform-design). An experiment here is evidence for a decision, not a decision.

## Experiments

*Each row is one experiment, with its question, the decision it informs, and its status.*

| Folder | Question | Decision it informs | Status |
| --- | --- | --- | --- |
| [engine-bakeoff](engine-bakeoff/README.md) | Can the Wax command path keep up with a large agency's nightly batch, and in which language and with which event store? | WAX-0025 Primary Programming Language, the decisions that name Marten as the event store, the batch path in WAX-0024, and the proposal to replace C# with Python | Round 1 complete, with [a report](engine-bakeoff/REPORT-ROUND-1.md); round 2 planned |

## How an experiment works

Each experiment lives in its own folder with a README that states the question, why it matters, the method, and the result. The method is committed before any result is recorded, so that the result cannot shape the test. When the question is answered, the README records the result and the decision it fed, and the folder is left as it stands.

## Licensing

All code in this repository is licensed under the [Apache License 2.0](LICENSE). Code adapted from other people's work is credited in [NOTICE](NOTICE) and in the README of the folder that contains it.

The license grants no rights to the Foundation's trademarks. Wax, HiveAR, and The OpenAR Collective are trademarks of The Open Accounts Receivable Collective Foundation, and their use is governed by the Foundation's Trademark Policy.

## Contributing

Contributions are accepted under the Developer Certificate of Origin, Version 1.1, with a `Signed-off-by` trailer on every commit. See [CONTRIBUTING.md](CONTRIBUTING.md).
