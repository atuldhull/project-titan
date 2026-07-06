"""Realtime WebSocket backend (TITAN-BCK-007, ADR-009).

The JWT-authenticated multiplexed ``/ws`` channel and its Redis-pub/sub fan-out:
domain events written to the Phase-10 outbox stream are republished by the
:mod:`~titan.realtime.fanout` consumer onto per-project / per-user / global
channels, and each api replica's :mod:`~titan.realtime.subscriber` forwards them
to its local sockets. Import through :mod:`titan.realtime.service`.
"""
