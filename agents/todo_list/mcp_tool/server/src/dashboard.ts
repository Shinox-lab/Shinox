import http from "node:http";
import { todoService } from "./services/TodoService.js";
import { databaseService } from "./services/DatabaseService.js";
import { config } from "./config.js";
import { Todo } from "./models/Todo.js";

const PORT = parseInt(process.env.TODO_DASHBOARD_PORT || "3456", 10);

function escapeHtml(str: string): string {
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function renderPage(): string {
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Todo Dashboard</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: #f5f5f7;
    color: #1d1d1f;
    line-height: 1.5;
  }
  .container { max-width: 900px; margin: 0 auto; padding: 24px 16px; }
  header { margin-bottom: 32px; }
  header h1 { font-size: 28px; font-weight: 700; }
  .stats {
    display: flex; gap: 16px; margin-top: 12px; font-size: 14px; color: #6e6e73;
  }
  .stats span { background: #fff; padding: 4px 12px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
  section { margin-bottom: 32px; }
  section h2 { font-size: 18px; font-weight: 600; margin-bottom: 12px; color: #1d1d1f; }
  .card {
    background: #fff; border-radius: 12px; padding: 16px 20px; margin-bottom: 12px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.08);
  }
  .card-header { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
  .dot {
    width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0;
  }
  .dot.active { background: #f5a623; }
  .dot.done { background: #34c759; }
  .card-title { font-weight: 600; font-size: 16px; }
  .card-desc {
    font-size: 14px; color: #6e6e73; margin-bottom: 8px;
    overflow: hidden; text-overflow: ellipsis;
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
  }
  .card-meta { font-size: 12px; color: #a1a1a6; display: flex; flex-wrap: wrap; gap: 12px; }
  .card-id { font-family: "SF Mono", SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace; font-size: 11px; color: #c7c7cc; margin-top: 6px; }
  .empty { color: #a1a1a6; font-size: 14px; font-style: italic; }
  footer { text-align: center; font-size: 12px; color: #a1a1a6; margin-top: 40px; }
  footer .refresh-indicator { display: inline-block; margin-left: 4px; }
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>Todo Dashboard</h1>
    <div class="stats">
      <span id="stat-total">Total: -</span>
      <span id="stat-active">Active: -</span>
      <span id="stat-completed">Completed: -</span>
    </div>
  </header>

  <section>
    <h2>Active</h2>
    <div id="active-list"><p class="empty">Loading...</p></div>
  </section>

  <section>
    <h2>Completed</h2>
    <div id="completed-list"><p class="empty">Loading...</p></div>
  </section>

  <footer>
    <span id="last-updated"></span>
    <span class="refresh-indicator">&#8226; auto-refreshes every 5s</span>
  </footer>
</div>

<script>
function esc(s) {
  var d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function fmtDate(iso) {
  if (!iso) return '';
  var d = new Date(iso);
  return d.toLocaleDateString() + ' ' + d.toLocaleTimeString();
}

function renderCard(todo) {
  var isDone = !!todo.completedAt;
  var dotClass = isDone ? 'done' : 'active';
  var desc = todo.description || '';
  if (desc.length > 200) desc = desc.slice(0, 200) + '...';

  var meta = '<span>Created: ' + esc(fmtDate(todo.createdAt)) + '</span>';
  meta += '<span>Updated: ' + esc(fmtDate(todo.updatedAt)) + '</span>';
  if (todo.completedAt) {
    meta += '<span>Completed: ' + esc(fmtDate(todo.completedAt)) + '</span>';
  }

  return '<div class="card">' +
    '<div class="card-header"><span class="dot ' + dotClass + '"></span><span class="card-title">' + esc(todo.title) + '</span></div>' +
    '<div class="card-desc">' + esc(desc) + '</div>' +
    '<div class="card-meta">' + meta + '</div>' +
    '<div class="card-id">' + esc(todo.id) + '</div>' +
  '</div>';
}

function refresh() {
  fetch('/api/todos')
    .then(function(r) { return r.json(); })
    .then(function(todos) {
      var active = todos.filter(function(t) { return !t.completedAt; });
      var completed = todos.filter(function(t) { return !!t.completedAt; });

      document.getElementById('stat-total').textContent = 'Total: ' + todos.length;
      document.getElementById('stat-active').textContent = 'Active: ' + active.length;
      document.getElementById('stat-completed').textContent = 'Completed: ' + completed.length;

      var activeEl = document.getElementById('active-list');
      activeEl.innerHTML = active.length
        ? active.map(renderCard).join('')
        : '<p class="empty">No active todos</p>';

      var completedEl = document.getElementById('completed-list');
      completedEl.innerHTML = completed.length
        ? completed.map(renderCard).join('')
        : '<p class="empty">No completed todos</p>';

      document.getElementById('last-updated').textContent =
        'Last updated: ' + new Date().toLocaleTimeString();
    })
    .catch(function(err) {
      console.error('Failed to fetch todos', err);
    });
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>`;
}

const server = http.createServer((req, res) => {
  if (req.method === "GET" && req.url === "/") {
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    res.end(renderPage());
    return;
  }

  if (req.method === "GET" && req.url === "/api/todos") {
    try {
      const todos: Todo[] = todoService.getAllTodos();
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(todos));
    } catch (err) {
      res.writeHead(500, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "Failed to fetch todos" }));
    }
    return;
  }

  res.writeHead(404, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ error: "Not found" }));
});

function shutdown() {
  console.error("Shutting down dashboard...");
  server.close(() => {
    databaseService.close();
    process.exit(0);
  });
  // Force exit after 5s if server hasn't closed
  setTimeout(() => process.exit(0), 5000);
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);

server.listen(PORT, () => {
  console.error(`SQLite database path: ${config.db.path}`);
  console.error(`Todo Dashboard running at http://localhost:${PORT}`);
});
