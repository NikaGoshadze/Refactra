# Refactra diagrams

Status: design drafts, exported 2026-11-16. They do not represent a deployed or tested system.

Keep all repository diagram assets in this folder. Editable Lucidchart and IcePanel documents remain the source for native edits; PNGs make the design visible directly from GitHub. The available sequence source is stored alongside its export.

## Three main diagrams

| Diagram | Repository export | Editable source |
|---|---|---|
| C4 container view | [c4-container.png](c4-container.png) | [IcePanel](https://app.icepanel.io/landscapes/SSnOOYKnW4Euzk6QScsJ/versions/latest/diagrams/editor?diagram=PjsRS1Dyam) |
| Sequential agent workflow | [agent-workflow.png](agent-workflow.png) | [Lucidchart](https://lucid.app/lucidchart/c925238c-f32c-4e9c-a2f8-2e9c9e775065/edit) |
| Task interactions | [task-sequence.png](task-sequence.png), [PlantUML source](task-sequence.puml) | [Lucidchart](https://lucid.app/lucidchart/2a5b1f77-10b1-489e-a2ce-447d2b2f37ca/edit) |

## Supporting design

| Diagram | Repository export | Editable source |
|---|---|---|
| Run data model | [run-data-model.png](run-data-model.png) | [Lucidchart](https://lucid.app/lucidchart/4619df40-ceb4-445f-8d40-411eb26ee35f/edit) |

The ERD is a supporting sketch, not a fourth main showcase diagram. Detailed architecture, contracts and decision records remain local until finalized.

## Updating

1. Edit the C4 model in IcePanel; edit swimlanes/sequence/ERD in Lucidchart.
2. Export the changed diagram into this folder using the existing filename. Update `task-sequence.puml` from Lucid's stored sequence markup when that diagram changes.
3. Inspect the new image and check it against current contracts/behavior. Update this export date and note any meaningful change.
4. Commit source/export changes with the related design or implementation change. Do not claim that a refreshed drawing proves runtime behavior.

Only sequence markup is available here as an editable text source. The PNGs are visual snapshots, not native editable Lucid/IcePanel files. Native document URLs above provide that edit path; no unsupported native export is implied.
