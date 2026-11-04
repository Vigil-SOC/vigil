# Adoption figures

Vigil publishes its adoption figures in a public repository,
[Vigil-SOC/metrics](https://github.com/Vigil-SOC/metrics). A scheduled job
reads GitHub's traffic and repository statistics for this repository once a
day and commits them there unaltered, so anyone can check what the project
claims and anyone can tell us where the measures fall short.

## What is published

- Views and unique visitors per day.
- Clones and unique cloners per day.
- Stars, forks, watchers, and open issues, recorded daily.
- Commit history for each contributor account, with bot and agent accounts
  flagged.
- A monthly summary derived from the daily files.
- A methodology note that defines each measure and its limits.
- A self-contained dashboard that renders the files above offline.

GitHub retains traffic data for fourteen days, so the traffic record begins
on 2026-07-31, the earliest day available when collection started, and cannot
reach further back. GitHub keeps no history of stars, forks, watchers, or open
issues, so that series begins on 2026-08-14, the day it was added.

## What is withheld

GitHub also reports referring sites and most visited paths. Those are collected
and kept private, because referrer data can carry the hostnames of
organizations that reach Vigil from internal systems, and those organizations
did not agree to be named.

## Why stars carry little weight

Stars cost one click and are sold by the thousand. Since mid-2026 only a
repository's administrators and collaborators can list the accounts behind a
star count, so outsiders cannot audit it. Evaluators should weigh the clone,
visitor, and contributor series instead, and should read the methodology note
before summing unique counts across days.

## Challenging a figure

Open an issue in [Vigil-SOC/metrics](https://github.com/Vigil-SOC/metrics/issues)
to question a number, propose a measure, or point out a flaw in the method.
