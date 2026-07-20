# BatchAgent Product

## Register

product

## Users

Developers, researchers, and automation operators who run many structurally similar agent tasks from a terminal. They need to start, leave, resume, inspect, and selectively retry long-running work without losing provenance or confusing a batch definition with one execution or one task attempt.

## Product Purpose

BatchAgent is a durable control plane for repeatable agent workloads. It turns Markdown batch configurations into observable runs, delegates individual tasks to an agent or external coding harness, validates submitted artifacts, and preserves the complete execution record. Success means an operator can always answer what was configured, what ran, what each attempt produced, how long it took, and what can safely happen next.

## Brand Personality

Precise, dependable, and calm. Copy should be direct and operational, with explicit identifiers and actionable states. The interface should feel like a trustworthy developer tool that stays out of the way while making consequential actions easy to verify.

## Anti-references

- Chat-first interfaces that hide scheduling, retries, or durable results inside an ephemeral transcript.
- Flat history views that conflate Batch Configs, Runs, Tasks, and Task Attempts.
- Decorative terminal dashboards with noisy color, unclear selection, or color-only status cues.
- Destructive retry or resume actions that silently overwrite earlier artifacts or history.
- Settings that appear to apply but disappear after the process exits.

## Design Principles

1. Preserve the hierarchy: navigate from Batch Config to Run to Task to Attempt, and show the relevant identifier at every level.
2. Persist operational truth: timing, token usage, results, settings, and errors survive restarts and are queryable from one state home.
3. Make the safe next action obvious: Run, Resume, Retry, and Rerun have distinct labels, eligibility rules, and consequences.
4. Use progressive disclosure: summaries lead to detailed event, message, tool, and artifact records without flattening them together.
5. Keep interfaces equivalent: TUI and non-interactive commands operate on the same scheduler state and persistence model.

## Accessibility & Inclusion

The TUI must be fully keyboard-operable, retain readable contrast across supported terminal themes, and never rely on color alone to communicate selection or status. Labels and timestamps use plain language and stable formats. Updates should avoid distracting animation, preserve scroll and focus, and remain usable in narrow terminals and with reduced-color environments.
