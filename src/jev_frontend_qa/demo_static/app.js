/* Synthetic Todo — frontend behaviour.
 *
 * Speaks only to /api/todos. All persistence flows through the backend so the
 * QA runner can observe real requests and verify real state, not in-memory
 * copies that vanish on reload. The UI is intentionally minimal: every
 * action is a real fetch() call, every error surfaces in a live region so the
 * runner (or a human with a screen reader) can see what happened.
 */

(() => {
  "use strict";

  const API_BASE = "/api/todos";
  const MAX_TITLE_LENGTH = 500;

  // -- DOM lookups ------------------------------------------------------------
  const createForm = document.getElementById("create-form");
  const createInput = document.getElementById("create-input");
  const createSubmit = document.getElementById("create-submit");
  const createStatus = document.getElementById("create-status");
  const list = document.getElementById("todo-list");
  const emptyState = document.getElementById("list-empty");
  const countValue = document.getElementById("todo-count-value");
  const dbIndicator = document.getElementById("db-indicator");
  const rowTemplate = document.getElementById("todo-row-template");
  const editTemplate = document.getElementById("todo-edit-template");

  // -- State ------------------------------------------------------------------
  /** @type {Map<string, HTMLElement>} */
  const rowsById = new Map();
  /** @type {Map<string, {form: HTMLFormElement, input: HTMLInputElement, status: HTMLElement}>} */
  const editsById = new Map();

  // -- Utilities --------------------------------------------------------------
  function setStatus(el, message, state) {
    el.textContent = message;
    if (state) {
      el.dataset.state = state;
    } else {
      delete el.dataset.state;
    }
  }

  function clearStatus(el) {
    setStatus(el, "", null);
  }

  function describeError(status, payload) {
    if (payload && typeof payload.error === "string" && payload.error.length > 0) {
      return payload.error;
    }
    return `Request failed with status ${status}`;
  }

  async function api(path, options = {}) {
    const init = {
      method: options.method || "GET",
      headers: { Accept: "application/json" },
    };
    if (options.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    let response;
    try {
      response = await fetch(API_BASE + path, init);
    } catch (networkError) {
      throw new Error(`Network error: ${networkError.message}`);
    }
    let payload = null;
    const text = await response.text();
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch (parseError) {
        payload = null;
      }
    }
    if (!response.ok) {
      const error = new Error(describeError(response.status, payload));
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  function isBlankTitle(value) {
    return typeof value !== "string" || value.trim().length === 0;
  }

  function announce(message, state) {
    if (!dbIndicator) return;
    dbIndicator.textContent = message;
    if (state) {
      dbIndicator.dataset.state = state;
    } else {
      delete dbIndicator.dataset.state;
    }
  }

  // -- Rendering --------------------------------------------------------------
  function renderTodo(todo) {
    const fragment = rowTemplate.content.cloneNode(true);
    const row = fragment.querySelector(".todo-row");
    row.dataset.todoId = todo.id;
    row.dataset.completed = todo.completed ? "true" : "false";
    const titleEl = row.querySelector('[data-testid="todo-title"]');
    titleEl.textContent = todo.title;
    const idEl = row.querySelector('[data-testid="todo-id"]');
    idEl.textContent = todo.id;
    const toggleBtn = row.querySelector('[data-action="toggle"]');
    const toggleLabel = todo.completed ? "Uncomplete" : "Complete";
    toggleBtn.textContent = toggleLabel;
    toggleBtn.setAttribute("aria-label", `${toggleLabel} ${todo.title}`);
    toggleBtn.setAttribute(
      "aria-pressed",
      todo.completed ? "true" : "false"
    );
    const editBtn = row.querySelector('[data-action="edit"]');
    editBtn.textContent = "Edit";
    editBtn.setAttribute("aria-label", `Edit ${todo.title}`);
    const deleteBtn = row.querySelector('[data-action="delete"]');
    deleteBtn.textContent = "Delete";
    deleteBtn.setAttribute("aria-label", `Delete ${todo.title}`);
    return row;
  }

  function replaceRow(todo) {
    const existing = rowsById.get(todo.id);
    if (!existing) return;
    const next = renderTodo(todo);
    existing.replaceWith(next);
    rowsById.set(todo.id, next);
  }

  function addRow(todo) {
    const row = renderTodo(todo);
    list.appendChild(row);
    rowsById.set(todo.id, row);
    syncListVisibility();
    syncCount();
  }

  function removeRow(todoId) {
    const row = rowsById.get(todoId);
    if (!row) return;
    const edit = editsById.get(todoId);
    if (edit) {
      edit.form.remove();
      editsById.delete(todoId);
    }
    row.remove();
    rowsById.delete(todoId);
    syncListVisibility();
    syncCount();
  }

  function syncListVisibility() {
    if (list.children.length === 0) {
      emptyState.hidden = false;
    } else {
      emptyState.hidden = true;
    }
  }

  function syncCount() {
    countValue.textContent = String(list.children.length);
  }

  // -- Create -----------------------------------------------------------------
  async function submitCreate(event) {
    event.preventDefault();
    const title = createInput.value;
    if (isBlankTitle(title)) {
      createInput.setAttribute("aria-invalid", "true");
      setStatus(
        createStatus,
        "Title must not be blank or whitespace only.",
        "error"
      );
      createInput.focus();
      return;
    }
    createInput.removeAttribute("aria-invalid");
    clearStatus(createStatus);
    createSubmit.disabled = true;
    try {
      const { todo } = await api("", {
        method: "POST",
        body: { title: title.trim() },
      });
      createInput.value = "";
      addRow(todo);
      setStatus(createStatus, `Created “${todo.title}”.`, "ok");
      announce("Database write succeeded.", "ok");
      // Move focus to the first interactive control of the new row so keyboard
      // users can continue without re-tabbing through the form.
      const created = rowsById.get(todo.id);
      const focusTarget = created?.querySelector('[data-action="toggle"]');
      focusTarget?.focus();
    } catch (error) {
      setStatus(createStatus, error.message, "error");
      announce(`Database write failed: ${error.message}`, "error");
      createInput.focus();
    } finally {
      createSubmit.disabled = false;
    }
  }

  // -- Toggle (complete / uncomplete) -----------------------------------------
  async function handleToggle(row) {
    const todoId = row.dataset.todoId;
    const nextCompleted = row.dataset.completed !== "true";
    const title =
      row.querySelector('[data-testid="todo-title"]')?.textContent || "todo";
    try {
      const { todo } = await api(`/${encodeURIComponent(todoId)}`, {
        method: "PATCH",
        body: { completed: nextCompleted },
      });
      replaceRow(todo);
      announce(
        nextCompleted
          ? `Marked “${title}” complete.`
          : `Marked “${title}” active.`,
        "ok"
      );
    } catch (error) {
      announce(`Update failed: ${error.message}`, "error");
    }
  }

  // -- Delete -----------------------------------------------------------------
  async function handleDelete(row) {
    const todoId = row.dataset.todoId;
    const title =
      row.querySelector('[data-testid="todo-title"]')?.textContent || "todo";
    try {
      await api(`/${encodeURIComponent(todoId)}`, { method: "DELETE" });
      removeRow(todoId);
      announce(`Deleted “${title}”.`, "ok");
    } catch (error) {
      announce(`Delete failed: ${error.message}`, "error");
    }
  }

  // -- Edit -------------------------------------------------------------------
  function beginEdit(row) {
    const todoId = row.dataset.todoId;
    const existing = editsById.get(todoId);
    if (existing) {
      existing.input.focus();
      existing.input.select();
      return;
    }
    const fragment = editTemplate.content.cloneNode(true);
    const form = fragment.querySelector(".todo-edit-form");
    const input = form.querySelector(".todo-edit__input");
    const status = form.querySelector(".status");
    const currentTitle =
      row.querySelector('[data-testid="todo-title"]')?.textContent || "";
    input.value = currentTitle;
    row.dataset.editing = "true";
    row.appendChild(form);
    editsById.set(todoId, { form, input, status });
    input.focus();
    input.select();
  }

  function cancelEdit(todoId) {
    const entry = editsById.get(todoId);
    if (!entry) return;
    entry.form.remove();
    editsById.delete(todoId);
    const row = rowsById.get(todoId);
    if (row) {
      delete row.dataset.editing;
    }
  }

  async function submitEdit(todoId) {
    const entry = editsById.get(todoId);
    if (!entry) return;
    const { input, status, form } = entry;
    const candidate = input.value;
    if (isBlankTitle(candidate)) {
      input.setAttribute("aria-invalid", "true");
      setStatus(
        status,
        "Title must not be blank or whitespace only.",
        "error"
      );
      input.focus();
      return;
    }
    input.removeAttribute("aria-invalid");
    clearStatus(status);
    const submitBtn = form.querySelector('button[type="submit"]');
    submitBtn.disabled = true;
    try {
      const { todo } = await api(`/${encodeURIComponent(todoId)}`, {
        method: "PATCH",
        body: { title: candidate.trim() },
      });
      cancelEdit(todoId);
      replaceRow(todo);
      announce(`Saved changes to “${todo.title}”.`, "ok");
    } catch (error) {
      setStatus(status, error.message, "error");
      announce(`Update failed: ${error.message}`, "error");
    } finally {
      submitBtn.disabled = false;
    }
  }

  // -- Event wiring -----------------------------------------------------------
  createForm.addEventListener("submit", submitCreate);
  createInput.addEventListener("input", () => {
    if (!isBlankTitle(createInput.value)) {
      createInput.removeAttribute("aria-invalid");
      clearStatus(createStatus);
    }
  });

  list.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    const button = target.closest("button[data-action]");
    if (!button) return;
    const row = button.closest(".todo-row");
    if (!row) return;
    const action = button.dataset.action;
    if (action === "toggle") {
      handleToggle(row);
    } else if (action === "edit") {
      beginEdit(row);
    } else if (action === "delete") {
      handleDelete(row);
    }
  });

  list.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (!form.matches(".todo-edit-form")) return;
    event.preventDefault();
    const row = form.closest(".todo-row");
    if (!row) return;
    submitEdit(row.dataset.todoId);
  });

  list.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    if (target.dataset.action === "cancel-edit") {
      const form = target.closest(".todo-edit-form");
      const row = form?.closest(".todo-row");
      if (row) cancelEdit(row.dataset.todoId);
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    for (const [todoId] of editsById) {
      cancelEdit(todoId);
    }
  });

  // -- Initial load -----------------------------------------------------------
  async function loadTodos() {
    try {
      const { todos } = await api("");
      const fragment = document.createDocumentFragment();
      for (const todo of todos) {
        fragment.appendChild(renderTodo(todo));
        rowsById.set(todo.id, fragment.lastElementChild);
      }
      list.replaceChildren(fragment);
      syncListVisibility();
      syncCount();
      announce(
        todos.length === 0
          ? "Database ready. No todos stored."
          : `Loaded ${todos.length} todo${todos.length === 1 ? "" : "s"} from database.`,
        "ok"
      );
    } catch (error) {
      announce(`Could not load todos: ${error.message}`, "error");
    }
  }

  loadTodos();
})();
