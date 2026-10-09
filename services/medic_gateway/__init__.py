"""Medic's read-only gateway: the only path between Medic and the Vigil backend.

Two listeners, two allow lists, stdlib only (C3 §4.6, A3-2, SP1):
  outbound  Medic -> gateway -> backend: exact GET list, the gateway's own Viewer login
  inbound   backend -> gateway -> Medic API: X2's reads + one POST, X-Medic-* headers
"""
