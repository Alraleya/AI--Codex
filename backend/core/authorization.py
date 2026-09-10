"""Shared authorization guard for provider-backed workflow actions."""


class ProviderCallNotAuthorized(RuntimeError):
    pass
