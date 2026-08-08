"""Generic graph entity and edge type vocabulary shared by the kernel and adapters.

The generic runtime (docs/adr/0008) owns the graph entity types that are
NOT category-specific: ``observation`` and ``evidence`` are seeded by the
runner, read by the reducer and specialists, and scanned by the HalCTF
flag candidate extractor. This module is their single source of truth —
kernel modules import from here, and category-specific adapters (e.g.
``ozzgraph.environments.halctf.flags``) import the generic types from here
too instead of redefining them (V09, docs/adr/0011: hints, submissions,
flags, and scoreboard moved OUT of the generic kernel into
``ozzgraph.environments.halctf``, so the kernel never imports a
category-specific module).

V09 note: before the halctf-adapter milestone these constants lived in
``ozzgraph.flags`` (a generic-kernel module that has since been moved to
``ozzgraph.environments.halctf.flags``). Generic types stay in the kernel;
only the HalCTF-specific vocabulary (``flag_candidate``, ``submission``,
``hint_purchase``, ...) lives with the adapter.
"""

from __future__ import annotations

#: Entity types the generic kernel reads and writes (docs/DATA_STRATEGY.md,
#: lowercase by convention).
ENTITY_OBSERVATION = "observation"
ENTITY_EVIDENCE = "evidence"

#: Edge types the generic kernel reads and writes (docs/DATA_STRATEGY.md,
#: uppercase by convention).
EDGE_EVIDENCE_EXTRACTED_FROM_OBSERVATION = "EVIDENCE EXTRACTED_FROM OBSERVATION"
