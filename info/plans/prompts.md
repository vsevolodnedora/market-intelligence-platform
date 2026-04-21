## Plan review prompt (gpt 5.4)

Diligently, section-by-section analyze the provided project plan as a principal quant engineer in finance would, and create a concise, technical, accurate report related to the key project ambiguities, invalid assumptions, weaknesses, or unjustifiable over-complications that would prevent it from being successful under stated limitations. 

Diligently validate your report against the online research relevant to the project. 


## plan consolidation prompt (claude)

Thoroughly, as a principle quant engineer at a hedge fund would, read the project plan idea (idea_10.md) and its 3rd party review, then carefully add valid elements from the extension plan into the project plan avoiding duplications, ambiguity and inconsistencies.

Validate the final plan before returning.


## Plan -> tickets (gpt 5.4)

Read and understand the provided project plan, diligently, section-by-section, as a principal quant engineer in finance would. 

Assuming that the project plan was signed off to be implemented by a team of experienced software engineers create a complete, technical, accurate list of tickets that the team needs to implement the project successfully (what to do / check / verify, how (explicitly, but concisely), and what are the success criteria and deliverables for each ticket). 

Finally, validate your list against the original plan ensuring it covers it completely accurately. 


## Review plan (claude)

Read and understand the provided project idea (ida_11.md) as well as the implementation plan (plan_11.md), diligently, section-by-section, as a principal quant engineer in finance would.   

Assuming that the project plan was signed off to be implemented by a team of experienced software engineers adjust the implementation plan (plan_11.md) so that it is correctly scoped, structured, detailed and allows to successfull implementation of the project. 

Validate the final plan against the original idea,  ensure self-consistency, and accuracy.



## Idea -> staged plan (gpt 5.4)

Read and understand the provided project plan, diligently, section-by-section, as a principal quant engineer in finance would. 
Assuming that the project plan was signed off to be implemented by a team of experienced software engineers create a complete, technical, accurate list of stages, each of which can be developed and implemented semi-independently by different team members, clearly defining 
1. stage specific goals and responsibilities,
2. externalities: docs / files that the stage might require for its configuration and a very abridged explanation how
2. connectivity: files and tables that the stage reads from / writes to 
3. success criteria and executive results (e.g., results that may require changes in the plan)

At the end of the plan, after each stage has been implemented and validated, write complete but concise "Cross-stage storage database schema" that clearly (but conciesly) describes the dataabase that the whole project is using so that the engineering team can design database interfaces for each stage independently.

Validate before returning that each stage of the plan is accurate and correct with respect to the original idea and is formulated in a way that would allow an experienced engineer to implement it and, its IO is properly connected with the rest of the plan.


## Idea -> staged plan format (gpt 5.4) 

I want to build a large project, event-driven (eventually), german tax-aware, algorithmic etf trading / allocation system (read provided file for details). I have created an overall idea file with project description, direction, constraints and basic research results (no in-depth investigations into validity or implementability). 

Now I need to convert this idea into a system that I can easily develop, change, understand and use in production. I intent to use AI for most of it, hence modularity (i.e., splitting the system into a set of loosely coupled components each of which can be given to AI to help implement, so that AI is only allowed to change code inside one module unless the contract is explicitly revised).

Example approach:
"
freeze interfaces, not implementations. Then build a thin shared kernel, explicit contracts, ledger-first storage, separate tax engine, bounded modules with owned state, and AI working module-by-module against frozen interfaces rather than against the whole system.  
Write events and postings first, derive state second.
"
In other words I am trying to identify the most optimal way to build a project where I do not have the complete view of the system myself and where the entire system is too large to be given as a context to AI, and hence has to be build in a way where its development can be naturally segmented. 

Give a concise, accurate, well thought-out answer. 


## Idea -> staged plan (gpt 5.4) [v2.0]

Read and understand the provided project plan, diligently, section-by-section, as a principal quant engineer in finance would. 

Assuming that the project plan was signed off to be implemented by a team of experienced software engineers create a complete, technical, implementation plan following:
1. kernel + modules structure so that 
   * the system is ledger-first,
   * module boundaries are stable and AI-friendly,
   * v1 remains a modular monolith,
   * and the later event-driven runtime is an implementation change, not an architectural rewrite.
   
2. Top-level architecture (one deployable service, one database, one process topology for v1.)
   * each module owns its own schema/tables,
   * all cross-module interaction goes through commands, domain events, and read APIs,
   * no module reads another module’s private tables,
   * all durable business facts are append-only,
   * all stateful projections are derived and rebuildable.

3. Proposed Modules such as 

| Module | Owns (§ in spec) | Emits | Consumes |
|---|---|---|---|
| **kernel** | types, IDs, event envelope, clock, bus, testkit | — | — |

Validate before returning that each stage of the plan is accurate and correct with respect to the original idea and is formulated in a way that would allow an experienced engineer to implement it and, its IO is properly connected with the rest of the plan.



## Exploration Prompt

I want to build a large project, event-driven (eventually), german tax-aware, algorithmic etf trading / allocation system (read provided file for details). I have created an overall idea file with project description, direction, constraints and basic research results (no in-depth investigations into validity or implementability). Now I need to convert this idea into a system that I can easily develop, change, understand and use in production. I intent to use AI for most of it, hence modularity (i.e., splitting the system into a set of loosely coupled components each of which can be given to AI to help implement, so that AI is only allowed to change code inside one module unless the contract is explicitly revised). Example approach: " freeze interfaces, not implementations. Then build a thin shared kernel, explicit contracts, ledger-first storage, separate tax engine, bounded modules with owned state, and AI working module-by-module against frozen interfaces rather than against the whole system. Write events and postings first, derive state second.

My current idea:

freeze interfaces, not implementations. 
Use a thin shared kernel, explicit contracts, ledger-first storage, separate tax engine, bounded modules with owned state, and AI working module-by-module against frozen interfaces rather than against the whole system.  
Write events and postings first, derive state second.  

* kernel + modules structure so that 
  * the system is ledger-first,
  * module boundaries are stable and AI-friendly,
  * v1 remains a modular monolith,
  * and the later event-driven runtime is an implementation change, not an architectural rewrite.
   
Shared kernel (`kernel.py`) — frozen on day 1

The kernel is deliberately thin. It contains only types, identifiers, and interfaces that every module needs and none owns. 
No business logic (tax, compliance, admission, fees) ever lives in the kernel.

For AI-driven development, fewer, harder boundaries are better than many small ones.  
Reduce the risk of the contract churn and coordination overhead.  

Each module lives in its own folder `modules/<name>/` containing one frozen `contract.py` (commands, events, query Protocols, error types, invariants) plus any number of internal implementation files that no other module may import.

| module                  | goal                                                                                      | owns                                                                                                                                                      | emits                                                                                                                              | consumes                                                                       | extended goals + responsibilities                                                                                                                                                                                                                                                                                 |
| ----------------------- | ----------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `kernel/`               | Frozen shared language only                                                               | IDs, typed money/time types, event envelope, `Clock`, `EventBus`, result/error types, shared enums                                                        | —                                                                                                                                  | —                                                                              | Keep this permanently thin. No tax, compliance, fee, settlement, or broker logic here. This is the only universally shared surface and should change rarely.                                                                                                                                                      |
| `platform_runtime/`     | Host and move facts safely, without becoming business logic                               | event persistence, replay runner, in-process bus, outbox/inbox, clocks, projection runner, schema migration helpers                                       | infrastructure-only signals if needed                                                                                              | all domain events                                                              | This absorbs `event_store`, `clock_adapter`, and projection plumbing. It is support code, not a business bounded context. v1 is synchronous in-process; v2 can swap transport/runtime here without changing domain contracts.                                                                                     |
| `market_policy/`        | Maintain dated facts and signed decisions that make trading legal/supported               | reference data, live-profile definitions, tradable-line records, evidence records, governance decisions, effective-dated parameters/config                | `ReferenceDatumVersioned`, `EvidenceRecorded`, `TradableLineApproved/Frozen/Unfrozen`, `ParameterPinned`, gate/exception decisions | external documents, reviewer decisions, broker/instrument metadata             | This is the merged home for `reference_data`, `evidence_registry`, `compliance`, `governance`, and `config_store`. They share the same effective-dated, evidence-backed decision model. Keeping them together reduces contract churn and matches your “signed decision + dated evidence + pinned params” pattern. |
| `execution/`            | Convert broker traffic into canonical operational facts                                   | broker parser rules, broker callback/archive, canonical order/execution identity, correction groups, execution-event ledger                               | `OrderSubmitted`, `BrokerOrderAck`, `BrokerFill`, `BrokerCorrection`, `StatementPosting`, terminal order events                    | `OrderAdmitted`, broker IO                                                     | This merges `broker_protocol`, `ibkr_adapter`, and `oms_execution_ledger`. It owns operational truth only: canonical IDs, correction grouping, fallback identity for non-API events, and raw-to-canonical broker normalization. It never becomes economic truth.                                                  |
| `portfolio_accounting/` | Own the sole economic truth and cash state                                                | append-only journal, postings, reserved-cash state, settled/trade-date/withdrawable cash views, settlement forecast state, fee/distribution booking state | `Posted`, `FillBooked`, `ReservedCashChanged`, `SettlementForecast`, journal-derived economic facts                                | execution facts, market policy refs, tax postings                              | This is the right home for `journal` plus the useful parts of `settlement_regime`. Your file says the local settlement engine is not the live authority; it is a forecast/reconciliation utility. That means it should sit next to journaled cash state, not as a peer top-level domain.                          |
| `admission_control/`    | Decide whether a live order is admissible right now                                       | admission decisions, reason codes, frozen decision inputs                                                                                                 | `OrderAdmitted`, `OrderRejected`                                                                                                   | strategy intents; queries into accounting, market policy, reconciliation       | Keep admission separate. It is the crossing point for settled-cash proof, minimum economic notional, supported-session/order-type rules, liquidity gate, re-submit budget, tier rules, and current strategy certification. This is a genuine business control boundary, not just application orchestration.       |
| `tax/`                  | Maintain Germany-specific tax state and its reconciliation to broker-posted tax cashflows | statutory ledger A, broker-tax-cash ledger B, bridge adjustments, annual reconciliation artifacts, tax exceptions                                         | `StatutoryTaxStateUpdated`, `TaxPosted`, `TaxReconciliationOpened/Closed`, `TaxExceptionOpened/Closed`                             | `FillBooked`, distributions, broker tax postings, market-policy facts/evidence | Keep tax separate. Your file explicitly requires two ledgers plus an exception workflow and manual annual close. That is too specialized and too legally distinct to bury inside accounting or policy.                                                                                                            |
| `ops_reconciliation/`   | Own cross-channel operational state and freeze logic                                      | Green/Amber/Red state per channel, divergence windows/bands, investigation records, break status                                                          | `ChannelStateChanged`, `ReconciliationBreak`, recovery events                                                                      | execution facts, accounting views, broker statements, tax/accounting outputs   | This stays top-level because it is not “just a report”: Amber changes decision inputs conservatively, Red freezes live admission, and channels span broker cash, positions, fees, and tax postings. That cross-cutting operational state deserves a dedicated owner.                                              |
| `strategy_simulation/`  | Produce candidate actions and falsify economics under the exact live rules                | strategy research configs, simulator runs, overlays, shadow decisions, ranking/rebalance logic                                                            | `StrategyIntent`, `RebalanceProposal`, simulation/report artifacts                                                                 | read/query models from accounting, market policy, ops reconciliation           | Merge `strategy_research`, `simulator`, and user-facing projections needed for research. Your file explicitly wants one canonical simulator with overlays, not multiple competing runtime modes. This module proposes; it never directly trades.                                                                  |

> Each module lives in its own folder modules/<name>/ containing one frozen contract.py (commands, events, query Protocols, error types, invariants) plus any number of internal implementation files that no other module may import.

In other words I am trying to identify the most optimal way to build a project where I do not have the complete view of the system myself and where the entire system is too large to be given as a context to AI, and hence has to be build in a way where its development can be naturally segmented. Give a concise, accurate, well thought-out answer.

