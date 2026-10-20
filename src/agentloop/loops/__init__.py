"""The five reconcile loops: dispatch, develop, debug, deploy, declutter.

Each module exposes ``run_tick(cfg)`` — one single-tick reconcile, mirroring the ``main`` of the
corresponding bash script. The interval runner in ``cli`` wraps a chosen ``run_tick`` in a
sleep loop.
"""
