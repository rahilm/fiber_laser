"""Optional parity profiles derived from ``ORIGINAL`` (docs/dev/ARCHITECTURE.md §3,
"Adding a modern algorithm").

Each module here defines and registers one :class:`~mlaser.parity_ports.profile.ParityProfile`;
:func:`~mlaser.parity_ports.profile.get_profile` imports this package when it is asked for a
name it does not know, so ``use_profile("<name>")`` works without an explicit import.  Importing
a profile module must stay cheap (optional libraries are imported on first use).

=================  ==========================================================================
``native``         ``"native-sampling"``: the sample -> quantise -> encode loop (and the
                   smoother's arithmetic) always in the C kernel ``mlaser._native``,
                   bit-identical to the ports; also the DEFAULT profile's kernel selection
                   (kernel after a self-check, else the ports; ``MLASER_NATIVE``,
                   ``kernel_status``)
``native_smooth``  the smoother's arithmetic on the kernel (crtmath, curve tables, path / plan
                   sampling): the DEFAULT profile's ``smooth_path_points`` (no profile of its
                   own)
``reference``      ``"original-reference"``: the statement-level reference implementations of
                   the kernels ``ORIGINAL`` runs as fast drop-ins (same output, bit for bit)
``modern_dxf``     ``"modern-dxf"``: the ``dxf`` slot on ``mlaser.io.dxf_modern`` (ezdxf
                   decompose / path / text; also ``import_dxf(path, mode="modern")``)
``modern_ortools`` ``"modern-ortools"``: sort mode 20 on OR-Tools routing (extension option,
                   no slot change; ``mlaser.cam.order_ortools``)
``modern_motion``  ``"modern-ruckig"``: own look-ahead + Ruckig Community per segment (motion
                   slot ``plan_velocity``).  NOT validated on hardware.
=================  ==========================================================================
"""
from . import modern_dxf, modern_motion, modern_ortools, native, reference  # noqa: F401  (register their profiles)
