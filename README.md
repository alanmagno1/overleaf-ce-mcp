# Overleaf Community Edition MCP Server & REST Bridge 🚀

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white)](https://github.com/alanmagno1/overleaf-ce-mcp)
[![MCP](https://img.shields.io/badge/Model_Context_Protocol-Compatible-purple)](https://modelcontextprotocol.io/)

A native **Model Context Protocol (MCP)** server and **REST API bridge** specifically built for self-hosted **Overleaf Community Edition (ShareLaTeX)**.

Connect AI coding assistants and agents (**Claude Desktop, Cursor, Antigravity, Windsurf**) directly to your private Overleaf instance to **explore, read, live-edit, patch, and compile** LaTeX documents with real-time browser synchronization and automated log diagnosis.

---

## ⚡ Why Overleaf Community Edition Needs This

Overleaf Community Edition (CE) is the go-to self-hosted solution for academic labs, universities, and privacy-conscious researchers. However:
1. **No Public API or Git Sync**: Overleaf reserves official REST APIs and Git integration (`git.overleaf.com`) for paid Cloud plans and *Server Pro* licenses.
2. **The Operational Transformation (OT) Trap**: Overleaf manages live editing in-memory and Redis through `document-updater`. Editing documents directly in MongoDB corrupts active sessions or gets overwritten.
3. **No Automated Compilations for AI**: Existing community MCP tools only do local Git commits—they cannot trigger the server-side TeX Live engine (`clsi`), parse LaTeX compile errors, or return the resulting PDF.

### What `overleaf-ce-mcp` does differently:
- ✅ **100% Native to Community Edition**: Works out of the box with standard `sharelatex/sharelatex` Docker deployments (tested on CE v5 & v6).
- ✅ **OT-Safe & Real-Time Sync**: Files written or patched through this bridge pass through Overleaf's upload pipeline. Connected browser tabs immediately see changes live on screen via WebSockets without disconnecting or corrupting the document.
- ✅ **Server-Side LaTeX Compilation**: Compiles projects using your instance's TeX Live environment (`pdflatex`, `xelatex`, `lualatex`).
- ✅ **Structured Error & Warning Extraction**: Automatically parses LaTeX `.log` files and extracts exact line numbers, error messages, and context snippets so your AI agent can auto-debug its own LaTeX code.
- ✅ **Direct PDF Delivery**: Generates authenticated URLs and streaming endpoints for instant PDF preview and download.
- ✅ **Protected & Dual Protocol**: Protected by `Authorization: Bearer <API_KEY>`, exposing both MCP (Streamable HTTP `/mcp` & SSE `/sse`) and a clean REST API.

---

## 📐 Architecture

```mermaid
flowchart LR
    subgraph Client["Your Local Machine"]
        AI["AI Assistant (Antigravity / Claude / Cursor)"]
    end

    subgraph Server["Your Server (Docker Network)"]
        Bridge["overleaf-ce-mcp (:8000)"]
        Overleaf["Overleaf Web (:80 / :3000)"]
        DocUpdater["document-updater (:3003)"]
        CLSI["CLSI / TeX Live (:3013)"]
        Redis[("Redis (:6379)")]
        Mongo[("MongoDB (:27017)")]
    end

    AI -- "MCP / REST (Bearer Auth)" --> Bridge
    Bridge -- "Session Injection" --> Redis
    Bridge -- "Project Metadata" --> Mongo
    Bridge -- "OT-Safe Upload / Sync" --> Overleaf
    Overleaf <--> DocUpdater
    Overleaf <--> Redis
    Overleaf -- "Compile LaTeX" --> CLSI
    DocUpdater <--> Mongo
```

---

## 🛠️ MCP Tools Reference

When connected, your AI assistant receives the following tools:

| Tool | Parameters | Description |
| :--- | :--- | :--- |
| `list_projects` | *None* | Lists all projects with their `project_id`, `name`, `compiler`, `rootDoc_id`, and `lastUpdated` timestamp. |
| `get_project_files` | `project_id` | Returns the complete hierarchical folder tree (`tree`), text doc IDs (`.tex`, `.bib`, `.cls`), and binary file IDs (`.png`, `.jpg`, `.pdf`). |
| `read_file` | `project_id`, `path_or_doc_id` | Fetches the live text content synchronized with `document-updater` (or Base64 data for binary files). Accepts doc ID or path (`"main.tex"`, `"sections/intro.tex"`). |
| `write_file` | `project_id`, `folder_id`, `filename`, `content`, `is_base64` | Creates or updates a file safely via Overleaf's OT upload engine without breaking active browser tabs. Supports text or Base64 images. |
| `patch_file` | `project_id`, `path_or_doc_id`, `old_str`, `new_str`, `replace_all` | Replaces an exact text block inside a document in real time. Perfect for targeted, low-latency edits. |
| `compile_project` | `project_id`, `root_doc_id`, `compiler`, `draft` | Compiles the LaTeX project, extracts parsed errors/warnings from `output.log`, and returns the PDF download URL. |
| `create_folder` | `project_id`, `parent_folder_id`, `name` | Creates a new folder in the project hierarchy. |
| `delete_entity` | `project_id`, `entity_type`, `entity_id` | Deletes a doc, binary file, or folder cleanly from Overleaf. |

---

## 🚀 Quick Start (Docker Compose)

### 1. Add `overleaf-mcp` to your Overleaf `docker-compose.yml`

In your existing Overleaf deployment directory (where `docker-compose.yml` or Overleaf Toolkit resides), add the `overleaf-mcp` service:

```yaml
services:
  # ... your existing overleaf, overleaf-mongo, overleaf-redis services ...

  overleaf-mcp:
    image: ghcr.io/alanmagno1/overleaf-ce-mcp:latest
    container_name: overleaf-mcp
    restart: unless-stopped
    mem_limit: 256m
    depends_on:
      overleaf:
        condition: service_started
      overleaf-mongo:
        condition: service_healthy
      overleaf-redis:
        condition: service_started
    ports:
      # Expose on your desired host port:
      - "8099:8000"
    environment:
      # Generate a secure key: openssl rand -hex 32
      OVERLEAF_MCP_API_KEY: "your-secret-api-key-here"

      # Internal Docker URLs (match your existing service names):
      OVERLEAF_WEB_URL: "http://overleaf:80"
      OVERLEAF_API_URL: "http://overleaf:3000"
      OVERLEAF_MONGO_URL: "mongodb://overleaf-mongo:27017/sharelatex?replicaSet=overleaf"
      OVERLEAF_REDIS_URL: "redis://overleaf-redis:6379/0"

      # Base URLs used to format PDF download links in compile outputs:
      OVERLEAF_PUBLIC_BASE_URL: "https://overleaf.example.com/mcp-bridge"
      OVERLEAF_TAILSCALE_BASE_URL: "http://your-server-ip:8099"
    networks:
      - default
      - overleaf-net
```

### 2. Start the service
```bash
docker compose up -d overleaf-mcp
```

### 3. Verify Health
```bash
curl http://localhost:8099/api/health
# {"status":"ok","service":"overleaf-ce-mcp"}
```

---

## 🌐 Exposing Over HTTPS (Reverse Proxy / Cloudflare Tunnel)

If your Overleaf instance is published behind a domain (e.g. `https://overleaf.example.com`), you can route `/mcp-bridge/` to the bridge container.

### Option A: Via Overleaf's Internal Nginx
Add this snippet to `/etc/nginx/vhost-extras/overleaf/mcp-bridge.conf` (or mount it in your compose file):

```nginx
location ^~ /mcp-bridge/ {
    resolver 127.0.0.11 valid=5s ipv6=off;
    set $mcp_upstream http://overleaf-mcp:8000;
    proxy_pass $mcp_upstream;

    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Connection "";

    # Critical for SSE (Server-Sent Events) and compilations:
    proxy_buffering off;
    proxy_cache off;
    chunked_transfer_encoding on;
    proxy_read_timeout 10m;
    proxy_send_timeout 10m;
}

location = /mcp-bridge {
    return 308 /mcp-bridge/;
}
```

Reload Nginx:
```bash
docker exec overleaf nginx -s reload
```

### Option B: Via Caddy
```caddy
overleaf.example.com {
    handle /mcp-bridge/* {
        reverse_proxy overleaf-mcp:8000 {
            flush_interval -1
        }
    }
    handle {
        reverse_proxy overleaf:80
    }
}
```

---

## 💻 Client Configuration

### Antigravity (`~/.gemini/antigravity/mcp_config.json`)
```json
{
  "mcpServers": {
    "overleaf": {
      "serverUrl": "https://overleaf.example.com/mcp-bridge/mcp",
      "headers": {
        "Authorization": "Bearer your-secret-api-key-here"
      }
    }
  }
}
```

### Claude Desktop (`claude_desktop_config.json`)
Using `mcp-remote` to bridge Streamable HTTP to Claude Desktop's stdio:
```json
{
  "mcpServers": {
    "overleaf": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://overleaf.example.com/mcp-bridge/mcp",
        "--header",
        "Authorization: Bearer your-secret-api-key-here"
      ]
    }
  }
}
```

### Cursor (`~/.cursor/mcp.json`)
```json
{
  "mcpServers": {
    "overleaf": {
      "url": "https://overleaf.example.com/mcp-bridge/mcp",
      "headers": {
        "Authorization": "Bearer your-secret-api-key-here"
      }
    }
  }
}
```

---

## 📡 REST API Reference

All REST endpoints accept `Authorization: Bearer <API_KEY>`, `X-API-Key: <API_KEY>`, or query param `?api_key=<API_KEY>`.

- `GET  /api/health`: Health status (public).
- `GET  /api/projects`: List all projects with metadata.
- `GET  /api/projects/{id}/files`: Get full folder tree and flat file/doc indices.
- `GET  /api/projects/{id}/file?path_or_doc_id=main.tex`: Read doc content in real time.
- `POST /api/projects/{id}/file`: Create or overwrite file (`folder_id`, `filename`, `content`, `is_base64`).
- `PATCH /api/projects/{id}/file`: Patch file (`path_or_doc_id`, `old_str`, `new_str`, `replace_all`).
- `POST /api/projects/{id}/compile`: Trigger compilation (`compiler`, `draft`, `root_doc_id`).
- `GET  /api/projects/{id}/pdf`: Download the latest compiled PDF binary.
- `GET  /api/projects/{id}/log`: Retrieve raw LaTeX compile `.log`.
- `POST /api/projects/{id}/folder`: Create subfolder (`parent_folder_id`, `name`).
- `DELETE /api/projects/{id}/{entity_type}/{entity_id}`: Delete entity (`doc`, `file`, `folder`).

---

## 🔒 Security Best Practices

1. **Keep `OVERLEAF_MCP_API_KEY` Strong**: Generate with `openssl rand -hex 32`.
2. **Do Not Expose Raw Ports to the Public Internet**: Expose the port only on your local network/VPN (Tailscale, WireGuard) or behind an authenticating reverse proxy with TLS (Cloudflare Tunnel, Caddy, Nginx).
3. **Container Isolation**: `overleaf-ce-mcp` only needs network access to `overleaf`, `overleaf-mongo`, and `overleaf-redis`. It does not need root privileges inside the container.

---

## 📄 License

Distributed under the [MIT License](LICENSE).

Authored by **[Alan Magno](https://github.com/alanmagno1)**.
