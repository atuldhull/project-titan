| agent | domain | authority | escalates_to | veto_power |
|-------|--------|-----------|--------------|------------|
| ceo | executive | final arbiter on cross domain disputes across the org | none | yes |
| architect | technical | resolves technical disputes and owns the architecture | ceo | no |
| pm_product | scope | resolves scope disputes and owns the product backlog | ceo | no |
| pm_project | priority | resolves priority disputes and owns the schedule | ceo | no |
| security | security | blocks merge and release on an unresolved veto | ceo | yes |
| qa | quality | rejects work that fails the acceptance criteria | pm_project | no |
| engineer | implementation | implements assigned tasks and raises disputes | architect | no |
| reviewer | quality | reviews diffs and approves or rejects each change | qa | no |
| memory_agent | brain | curates durable memory records for the whole org | architect | no |
| orchestrator | runtime | schedules turns and enforces discussion deadlines | ceo | no |
| researcher | discovery | gathers external context and cites its sources | architect | no |
| designer | product | produces interface specifications and mockups | pm_product | no |
| release_manager | delivery | cuts releases once security has cleared them | ceo | no |
| data_steward | governance | enforces retention and scope isolation rules | security | no |
| planner | strategy | decomposes briefs into tasks and milestones | pm_project | no |
| estimator | strategy | sizes tasks and flags unrealistic schedules | pm_project | no |
| integrator | delivery | merges approved diffs and resolves conflicts | architect | no |
| observer | runtime | watches health metrics and raises incidents | orchestrator | no |
| scribe | brain | records decision logs and meeting summaries | memory_agent | no |
| auditor | governance | verifies ledgers are append only and complete | data_steward | no |
