"""Contract: the MCP endpoint only serves requests carrying a valid token from the configured
OIDC provider, and tells clients where to log in.

The provider is a local HTTP server publishing a real JWKS; tokens are real signed JWTs.
"""

import json
import socket
import socketserver
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anyio
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings

from czn_data.server import OidcTokenVerifier, build_server

AUDIENCE = "czn-mcp"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def provider():
    """A minimal OIDC provider: signing key, issuer URL, JWKS endpoint."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwks = json.dumps({"keys": [jwk | {"kid": "k1", "use": "sig", "alg": "RS256"}]}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(jwks)

        def log_message(self, *args):
            pass

    class Server(ThreadingHTTPServer):
        def server_bind(self):
            # HTTPServer.server_bind resolves the host's FQDN, which can stall for 30 s.
            socketserver.TCPServer.server_bind(self)
            self.server_name, self.server_port = self.server_address[:2]

    server = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    issuer = f"http://127.0.0.1:{server.server_port}"

    def token(**overrides) -> str:
        claims = {"iss": issuer, "aud": AUDIENCE, "sub": "robert", "exp": time.time() + 300}
        claims |= overrides
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})

    yield issuer, f"{issuer}/jwks", token
    server.shutdown()


def test_verifier_accepts_valid_tokens_and_rejects_others(provider):
    issuer, jwks_uri, token = provider
    verifier = OidcTokenVerifier(issuer, AUDIENCE, jwks_uri)
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(
        {"iss": issuer, "aud": AUDIENCE, "exp": time.time() + 300},
        other_key,
        algorithm="RS256",
        headers={"kid": "k1"},
    )

    async def verify(value):
        return await verifier.verify_token(value)

    accepted = anyio.run(verify, token(scope="czn:read"))
    assert accepted.subject == "robert"
    assert accepted.scopes == ["czn:read"]
    assert anyio.run(verify, token(aud="someone-else")) is None
    assert anyio.run(verify, token(iss="https://evil.example")) is None
    assert anyio.run(verify, token(exp=time.time() - 60)) is None
    assert anyio.run(verify, forged) is None


@pytest.fixture(scope="module")
def mcp_url(provider):
    issuer, jwks_uri, _ = provider
    port = free_port()
    public_url = f"http://127.0.0.1:{port}/mcp"
    auth = AuthSettings(
        issuer_url=issuer,
        resource_server_url=public_url,
        required_scopes=[],
        validate_token_resource=False,
    )
    app = build_server(auth, OidcTokenVerifier(issuer, AUDIENCE, jwks_uri)).streamable_http_app(
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    yield public_url
    server.should_exit = True
    thread.join()


def post_initialize(url: str, token: str | None):
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        }
    ).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers)


def test_endpoint_rejects_missing_or_bad_tokens_and_points_to_the_login(mcp_url, provider):
    for token in (None, "not-a-jwt", provider[2](aud="someone-else")):
        status, headers = post_initialize(mcp_url, token)
        assert status == 401
        assert "resource_metadata=" in headers["www-authenticate"]

    metadata_url = mcp_url.replace("/mcp", "/.well-known/oauth-protected-resource/mcp")
    with urllib.request.urlopen(metadata_url, timeout=10) as response:
        metadata = json.load(response)
    assert metadata["authorization_servers"] == [provider[0]]


def test_endpoint_serves_valid_tokens(mcp_url, provider):
    status, _ = post_initialize(mcp_url, provider[2]())
    assert status == 200


def test_health_check_needs_no_token(mcp_url):
    with urllib.request.urlopen(mcp_url.replace("/mcp", "/healthz"), timeout=10) as response:
        assert response.status == 200
