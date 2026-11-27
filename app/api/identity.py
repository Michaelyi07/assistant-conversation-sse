"""Identity stub.

Connection point (S2-IAM): replace ``get_identity`` with the real IAM
dependency (auth token validation, role and data-scope resolution). The stub
reads plain headers so integration tests can drive identities without auth.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request


@dataclass
class Identity:
    user_id: str
    org_id: str
    site_id: str
    role_description: str = "employee (demo identity stub)"


async def get_identity(request: Request) -> Identity:
    return Identity(
        user_id=request.headers.get("X-User-Id", "demo-user"),
        org_id=request.headers.get("X-Org-Id", "demo-org"),
        site_id=request.headers.get("X-Site-Id", "demo-site"),
    )
