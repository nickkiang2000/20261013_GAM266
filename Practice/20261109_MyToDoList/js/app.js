// Main Application Entry & Controller
import {
  activeConfig,
  isConfigComplete,
  saveConfig,
  resetConfig
} from "./firebase-config.js";

import {
  loginWithGoogle,
  logoutUser,
  subscribeAuth,
  getCurrentUser
} from "./auth.js";

import {
  subscribeUserLists,
  createList,
  updateList,
  deleteList,
  addCollaborator,
  removeCollaborator,
  joinList,
  subscribeListTodos,
  createTodo,
  updateTodo,
  deleteTodo,
  toggleTodoStatus,
  getListById
} from "./db.js";

// Application State
const state = {
  currentUser: null,
  activeListId: null,
  lists: [],
  todos: [],
  filterStatus: "all", // 'all' | 'active' | 'completed'
  filterPriority: "all",
  searchQuery: "",
  quickFilter: "all", // 'all' | 'assigned-me' | 'due-today'
  unsubLists: null,
  unsubTodos: null,
  pendingJoinListId: null
};

// ==========================================
// Toast Notification Utility
// ==========================================
export function showToast(message, type = "info") {
  const container = document.getElementById("toast-container");
  if (!container) return;

  const toast = document.createElement("div");
  const bgColors = {
    info: "bg-slate-800 text-white",
    success: "bg-emerald-600 text-white",
    error: "bg-rose-600 text-white",
    warning: "bg-amber-600 text-white"
  };

  const icons = {
    info: "info",
    success: "check-circle-2",
    error: "alert-circle",
    warning: "alert-triangle"
  };

  toast.className = `pointer-events-auto flex items-center space-x-2.5 px-4 py-3 rounded-xl shadow-lg text-xs font-medium transform transition-all duration-300 translate-y-2 opacity-0 ${bgColors[type] || bgColors.info}`;
  toast.innerHTML = `
    <i data-lucide="${icons[type] || 'info'}" class="w-4 h-4 flex-shrink-0"></i>
    <span>${escapeHtml(message)}</span>
  `;

  container.appendChild(toast);
  lucide.createIcons({ root: toast });

  requestAnimationFrame(() => {
    toast.classList.remove("translate-y-2", "opacity-0");
  });

  setTimeout(() => {
    toast.classList.add("translate-y-2", "opacity-0");
    setTimeout(() => toast.remove(), 300);
  }, 3500);
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

// ==========================================
// Initialization
// ==========================================
document.addEventListener("DOMContentLoaded", () => {
  lucide.createIcons();

  // Check URL parameters for list invitation (?join=LIST_ID)
  const urlParams = new URLSearchParams(window.location.search);
  if (urlParams.has("join")) {
    state.pendingJoinListId = urlParams.get("join");
  }

  setupEventListeners();
  checkFirebaseStatus();
  initAuthListener();
});

function checkFirebaseStatus() {
  const dot = document.getElementById("firebase-status-dot");
  const banner = document.getElementById("config-warning-banner");

  if (isConfigComplete()) {
    if (dot) dot.className = "w-2 h-2 rounded-full bg-emerald-500 ml-1";
    if (banner) banner.classList.add("hidden");
  } else {
    if (dot) dot.className = "w-2 h-2 rounded-full bg-amber-500 ml-1 animate-pulse";
    if (banner) banner.classList.remove("hidden");
  }
}

// ==========================================
// Authentication Handling
// ==========================================
function initAuthListener() {
  subscribeAuth(async (user) => {
    state.currentUser = user;
    renderAuthUI(user);

    if (user) {
      document.getElementById("view-logged-out").classList.add("hidden");
      document.getElementById("view-dashboard").classList.remove("hidden");

      // Handle pending invite if present
      if (state.pendingJoinListId) {
        await handleJoinInvite(state.pendingJoinListId);
        state.pendingJoinListId = null;
        // Clean URL query param without refresh
        window.history.replaceState({}, document.title, window.location.pathname);
      }

      startListsSubscription(user.email);
    } else {
      document.getElementById("view-logged-out").classList.remove("hidden");
      document.getElementById("view-dashboard").classList.add("hidden");

      if (state.unsubLists) {
        state.unsubLists();
        state.unsubLists = null;
      }
      if (state.unsubTodos) {
        state.unsubTodos();
        state.unsubTodos = null;
      }
    }
    lucide.createIcons();
  });
}

function renderAuthUI(user) {
  const container = document.getElementById("auth-container");
  if (!container) return;

  if (user) {
    container.innerHTML = `
      <div class="flex items-center space-x-2">
        <img src="${user.photoURL}" alt="${escapeHtml(user.displayName)}" class="w-8 h-8 rounded-full border border-slate-200">
        <span class="hidden md:inline text-xs font-semibold text-slate-700">${escapeHtml(user.displayName)}</span>
      </div>
    `;

    // Footer info
    const footerAvatar = document.getElementById("user-avatar-footer");
    const footerName = document.getElementById("user-name-footer");
    const footerEmail = document.getElementById("user-email-footer");

    if (footerAvatar) footerAvatar.src = user.photoURL;
    if (footerName) footerName.textContent = user.displayName;
    if (footerEmail) footerEmail.textContent = user.email;
  } else {
    container.innerHTML = `
      <button id="btn-header-login" class="inline-flex items-center space-x-2 px-3 py-1.5 rounded-lg text-xs font-semibold bg-brand-600 hover:bg-brand-700 text-white transition-colors shadow-xs">
        <i data-lucide="log-in" class="w-3.5 h-3.5"></i>
        <span>Google 登入</span>
      </button>
    `;
    const btn = document.getElementById("btn-header-login");
    if (btn) btn.addEventListener("click", handleGoogleLogin);
  }
}

async function handleGoogleLogin() {
  try {
    showToast("正在連線 Google 登入...", "info");
    const user = await loginWithGoogle();
    showToast(`歡迎回來，${user.displayName}！`, "success");
  } catch (err) {
    showToast(err.message || "登入失敗", "error");
    if (!isConfigComplete()) {
      openModal("modal-firebase-settings");
    }
  }
}

async function handleJoinInvite(listId) {
  try {
    const res = await joinList(listId, state.currentUser);
    if (res.alreadyJoined) {
      showToast(`您已經是「${res.list.title}」的協作成員`, "info");
    } else {
      showToast(`成功加入協作清單「${res.list.title}」！`, "success");
    }
    selectList(listId);
  } catch (err) {
    showToast(err.message || "無法加入清單", "error");
  }
}

// ==========================================
// Lists Subscription & Management
// ==========================================
function startListsSubscription(userEmail) {
  if (state.unsubLists) state.unsubLists();

  state.unsubLists = subscribeUserLists(
    userEmail,
    (lists) => {
      state.lists = lists;
      renderLists();
      updateQuickFilterBadges();

      // If active list not selected or active list was deleted, select first list
      if (!state.activeListId || !lists.some((l) => l.id === state.activeListId)) {
        if (lists.length > 0) {
          selectList(lists[0].id);
        } else {
          state.activeListId = null;
          renderActiveListHeader(null);
          renderTodos();
        }
      } else {
        // Refresh active list header info (collaborator changes, title changes)
        const activeList = lists.find((l) => l.id === state.activeListId);
        renderActiveListHeader(activeList);
      }
    },
    (err) => {
      showToast("載入清單失敗：" + err.message, "error");
    }
  );
}

function selectList(listId) {
  state.activeListId = listId;
  const list = state.lists.find((l) => l.id === listId);

  renderLists();
  renderActiveListHeader(list);

  // Subscribe to this list's todos
  if (state.unsubTodos) state.unsubTodos();

  state.unsubTodos = subscribeListTodos(
    listId,
    (todos) => {
      state.todos = todos;
      renderTodos();
      updateProgressBar();
      updateQuickFilterBadges();
      populateAssigneeDropdowns();
    },
    (err) => {
      showToast("載入待辦事項失敗：" + err.message, "error");
    }
  );
}

function renderLists() {
  const container = document.getElementById("lists-container");
  if (!container) return;

  if (state.lists.length === 0) {
    container.innerHTML = `
      <div class="text-center py-8 px-2 text-slate-400">
        <i data-lucide="folder-plus" class="w-8 h-8 mx-auto mb-2 opacity-50"></i>
        <p class="text-xs">尚無協作清單</p>
        <button id="btn-empty-create-list" class="mt-2 text-brand-600 hover:text-brand-700 text-xs font-medium underline">
          立即建立第一張清單
        </button>
      </div>
    `;
    const btn = document.getElementById("btn-empty-create-list");
    if (btn) btn.addEventListener("click", () => openCreateListModal());
    lucide.createIcons({ root: container });
    return;
  }

  container.innerHTML = state.lists
    .map((list) => {
      const isActive = list.id === state.activeListId;
      const isOwner = list.ownerEmail === state.currentUser?.email?.toLowerCase();
      const memberCount = list.memberEmails ? list.memberEmails.length : 1;

      return `
        <div data-list-id="${list.id}" class="list-item group flex items-center justify-between px-3 py-2.5 rounded-xl text-xs font-medium cursor-pointer transition-all ${
          isActive
            ? "bg-brand-50/80 text-brand-700 font-semibold shadow-xs border border-brand-100"
            : "text-slate-600 hover:bg-slate-100/80"
        }">
          <div class="flex items-center space-x-2.5 min-w-0">
            <span class="text-base flex-shrink-0">${list.icon || "📋"}</span>
            <div class="truncate">
              <span class="truncate block">${escapeHtml(list.title)}</span>
              ${!isOwner ? `<span class="text-[10px] text-slate-400">由 ${escapeHtml(list.ownerName || '他人')} 建立</span>` : ''}
            </div>
          </div>
          <div class="flex items-center space-x-1 flex-shrink-0">
            <span class="inline-flex items-center space-x-0.5 text-[10px] text-slate-400 bg-slate-100 px-1.5 py-0.5 rounded-full" title="${memberCount} 位成員">
              <i data-lucide="users" class="w-2.5 h-2.5"></i>
              <span>${memberCount}</span>
            </span>
          </div>
        </div>
      `;
    })
    .join("");

  // Attach click events
  container.querySelectorAll(".list-item").forEach((el) => {
    el.addEventListener("click", () => {
      const id = el.getAttribute("data-list-id");
      selectList(id);
      // Close mobile sidebar if open
      closeMobileSidebar();
    });
  });

  lucide.createIcons({ root: container });
}

function renderActiveListHeader(list) {
  const iconEl = document.getElementById("active-list-icon");
  const titleEl = document.getElementById("active-list-title");
  const descEl = document.getElementById("active-list-desc");
  const ownerBadge = document.getElementById("active-list-owner-badge");
  const avatarGroup = document.getElementById("collaborators-avatar-group");
  const mobileTitle = document.getElementById("mobile-header-title");

  if (!list) {
    if (titleEl) titleEl.textContent = "請建立或選擇清單";
    if (descEl) descEl.textContent = "點擊左側「+ 新增清單」開始多人協作";
    if (iconEl) iconEl.textContent = "📝";
    if (avatarGroup) avatarGroup.innerHTML = "";
    if (ownerBadge) ownerBadge.classList.add("hidden");
    return;
  }

  const isOwner = list.ownerEmail === state.currentUser?.email?.toLowerCase();

  if (iconEl) {
    iconEl.textContent = list.icon || "📋";
    iconEl.style.backgroundColor = `${list.color || '#6366f1'}15`;
    iconEl.style.borderColor = `${list.color || '#6366f1'}30`;
  }
  if (titleEl) titleEl.textContent = list.title;
  if (mobileTitle) mobileTitle.textContent = list.title;
  if (descEl) descEl.textContent = list.description || "即時多人協作待辦事項";

  if (ownerBadge) {
    if (isOwner) {
      ownerBadge.classList.remove("hidden");
      ownerBadge.textContent = "清單建立者";
    } else {
      ownerBadge.classList.remove("hidden");
      ownerBadge.textContent = `建立者: ${list.ownerName || list.ownerEmail}`;
    }
  }

  // Render collaborator avatars
  if (avatarGroup) {
    const members = list.members || [];
    const maxShow = 4;
    const displayed = members.slice(0, maxShow);
    const extraCount = members.length - maxShow;

    avatarGroup.innerHTML = displayed
      .map(
        (m) => `
        <img class="inline-block h-7 w-7 rounded-full ring-2 ring-white border border-slate-200" 
             src="${m.photoURL || `https://ui-avatars.com/api/?name=${encodeURIComponent(m.displayName || m.email)}&background=6366f1&color=fff`}" 
             alt="${escapeHtml(m.displayName || m.email)}" 
             title="${escapeHtml(m.displayName || m.email)} (${m.role === 'owner' ? '擁有者' : '協作者'})">
      `
      )
      .join("") +
      (extraCount > 0
        ? `<div class="inline-flex items-center justify-center h-7 w-7 rounded-full bg-slate-200 ring-2 ring-white text-[10px] font-bold text-slate-600">+${extraCount}</div>`
        : "");
  }
}

function updateProgressBar() {
  const total = state.todos.length;
  const completed = state.todos.filter((t) => t.completed).length;
  const percent = total === 0 ? 0 : Math.round((completed / total) * 100);

  const fillEl = document.getElementById("progress-bar-fill");
  const textEl = document.getElementById("progress-text");

  if (fillEl) fillEl.style.width = `${percent}%`;
  if (textEl) textEl.textContent = `${percent}% (${completed}/${total})`;
}

function updateQuickFilterBadges() {
  const allCount = state.todos.length;
  const userEmail = state.currentUser?.email?.toLowerCase();
  const todayStr = new Date().toISOString().split("T")[0];

  const assignedCount = state.todos.filter(
    (t) => t.assignedTo && t.assignedTo.email?.toLowerCase() === userEmail && !t.completed
  ).length;

  const dueTodayCount = state.todos.filter(
    (t) => t.dueDate === todayStr && !t.completed
  ).length;

  const bAll = document.getElementById("badge-count-all");
  const bAssigned = document.getElementById("badge-count-assigned");
  const bToday = document.getElementById("badge-count-today");

  if (bAll) bAll.textContent = allCount;
  if (bAssigned) bAssigned.textContent = assignedCount;
  if (bToday) bToday.textContent = dueTodayCount;
}

// ==========================================
// To-Do Rendering & Filtering
// ==========================================
function renderTodos() {
  const container = document.getElementById("tasks-container");
  const emptyState = document.getElementById("tasks-empty-state");
  if (!container) return;

  if (!state.activeListId) {
    container.innerHTML = "";
    if (emptyState) emptyState.classList.remove("hidden");
    return;
  }

  // Filter tasks
  const filtered = state.todos.filter((todo) => {
    // 1. Status Filter
    if (state.filterStatus === "active" && todo.completed) return false;
    if (state.filterStatus === "completed" && !todo.completed) return false;

    // 2. Priority Filter
    if (state.filterPriority !== "all" && todo.priority !== state.filterPriority) return false;

    // 3. Quick Filter from sidebar
    const userEmail = state.currentUser?.email?.toLowerCase();
    const todayStr = new Date().toISOString().split("T")[0];
    if (state.quickFilter === "assigned-me") {
      if (!todo.assignedTo || todo.assignedTo.email?.toLowerCase() !== userEmail) return false;
    } else if (state.quickFilter === "due-today") {
      if (todo.dueDate !== todayStr) return false;
    }

    // 4. Search Filter
    if (state.searchQuery.trim()) {
      const q = state.searchQuery.toLowerCase();
      const matchTitle = (todo.title || "").toLowerCase().includes(q);
      const matchNotes = (todo.notes || "").toLowerCase().includes(q);
      const matchAssignee = (todo.assignedTo?.displayName || "").toLowerCase().includes(q);
      if (!matchTitle && !matchNotes && !matchAssignee) return false;
    }

    return true;
  });

  if (filtered.length === 0) {
    container.innerHTML = "";
    if (emptyState) emptyState.classList.remove("hidden");
  } else {
    if (emptyState) emptyState.classList.add("hidden");
    container.innerHTML = filtered.map((todo) => renderTodoRow(todo)).join("");
    attachTodoEvents(container);
  }

  lucide.createIcons({ root: container });
}

function renderTodoRow(todo) {
  const isCompleted = Boolean(todo.completed);
  const priorityBadges = {
    high: '<span class="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-rose-50 text-rose-600 border border-rose-200">高</span>',
    medium: '<span class="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-amber-50 text-amber-600 border border-amber-200">中</span>',
    low: '<span class="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-blue-50 text-blue-600 border border-blue-200">低</span>'
  };

  // Due Date Badge
  let dueDateBadge = "";
  if (todo.dueDate) {
    const todayStr = new Date().toISOString().split("T")[0];
    const isOverdue = !isCompleted && todo.dueDate < todayStr;
    const isToday = !isCompleted && todo.dueDate === todayStr;

    let dateClass = "bg-slate-100 text-slate-600";
    if (isOverdue) dateClass = "bg-rose-100 text-rose-700 font-semibold";
    else if (isToday) dateClass = "bg-amber-100 text-amber-800 font-semibold";

    dueDateBadge = `
      <span class="inline-flex items-center space-x-1 px-2 py-0.5 rounded-md text-[10px] ${dateClass}">
        <i data-lucide="clock" class="w-3 h-3"></i>
        <span>${todo.dueDate}${isOverdue ? ' (逾期)' : isToday ? ' (今天)' : ''}</span>
      </span>
    `;
  }

  // Assignee Badge
  let assigneeBadge = "";
  if (todo.assignedTo) {
    assigneeBadge = `
      <div class="inline-flex items-center space-x-1.5 px-2 py-0.5 rounded-full bg-slate-100 text-slate-700 text-[10px] border border-slate-200" title="負責人: ${escapeHtml(todo.assignedTo.displayName || todo.assignedTo.email)}">
        <img class="w-3.5 h-3.5 rounded-full" src="${todo.assignedTo.photoURL || `https://ui-avatars.com/api/?name=${encodeURIComponent(todo.assignedTo.displayName || 'U')}&background=6366f1&color=fff`}">
        <span class="max-w-[70px] truncate">${escapeHtml(todo.assignedTo.displayName || todo.assignedTo.email)}</span>
      </div>
    `;
  }

  return `
    <div data-todo-id="${todo.id}" class="group bg-white rounded-xl p-3 sm:p-4 border border-slate-200/80 hover:border-slate-300 hover:shadow-xs transition-all flex items-start space-x-3 ${isCompleted ? 'opacity-70 bg-slate-50/50' : ''}">
      
      <!-- Checkbox -->
      <button type="button" class="btn-toggle-todo mt-0.5 flex-shrink-0 w-5 h-5 rounded-md border flex items-center justify-center transition-all ${
        isCompleted
          ? "bg-brand-600 border-brand-600 text-white"
          : "border-slate-300 hover:border-brand-500 bg-white"
      }">
        ${isCompleted ? '<i data-lucide="check" class="w-3.5 h-3.5 stroke-[3]"></i>' : ''}
      </button>

      <!-- Task Content -->
      <div class="flex-1 min-w-0">
        <div class="flex items-start justify-between gap-2">
          <div class="min-w-0">
            <p class="text-sm font-medium ${isCompleted ? 'task-title-completed text-slate-400' : 'text-slate-800'} break-words">
              ${escapeHtml(todo.title)}
            </p>
            ${
              todo.notes
                ? `<p class="text-xs text-slate-500 mt-0.5 line-clamp-2">${escapeHtml(todo.notes)}</p>`
                : ''
            }
          </div>

          <!-- Hover Actions -->
          <div class="opacity-0 group-hover:opacity-100 flex items-center space-x-1 flex-shrink-0 transition-opacity">
            <button class="btn-edit-todo p-1 text-slate-400 hover:text-brand-600 hover:bg-slate-100 rounded-md transition-colors" title="編輯事項">
              <i data-lucide="edit-2" class="w-3.5 h-3.5"></i>
            </button>
            <button class="btn-delete-todo p-1 text-slate-400 hover:text-rose-600 hover:bg-rose-50 rounded-md transition-colors" title="刪除事項">
              <i data-lucide="trash-2" class="w-3.5 h-3.5"></i>
            </button>
          </div>
        </div>

        <!-- Badges & Creator Info -->
        <div class="mt-2 flex flex-wrap items-center gap-2 text-xs">
          ${priorityBadges[todo.priority] || priorityBadges.medium}
          ${dueDateBadge}
          ${assigneeBadge}
          <span class="text-[10px] text-slate-400">
            建立者: ${escapeHtml(todo.createdBy?.displayName || '成員')}
          </span>
        </div>
      </div>

    </div>
  `;
}

function attachTodoEvents(container) {
  // Toggle Status
  container.querySelectorAll(".btn-toggle-todo").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const row = btn.closest("[data-todo-id]");
      const todoId = row.getAttribute("data-todo-id");
      const todo = state.todos.find((t) => t.id === todoId);
      if (!todo) return;

      try {
        await toggleTodoStatus(state.activeListId, todoId, todo.completed);
      } catch (err) {
        showToast("更新狀態失敗：" + err.message, "error");
      }
    });
  });

  // Edit Task
  container.querySelectorAll(".btn-edit-todo").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const row = btn.closest("[data-todo-id]");
      const todoId = row.getAttribute("data-todo-id");
      const todo = state.todos.find((t) => t.id === todoId);
      if (todo) openEditTaskModal(todo);
    });
  });

  // Delete Task
  container.querySelectorAll(".btn-delete-todo").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const row = btn.closest("[data-todo-id]");
      const todoId = row.getAttribute("data-todo-id");
      if (!confirm("確定要刪除這筆待辦事項嗎？")) return;

      try {
        await deleteTodo(state.activeListId, todoId);
        showToast("已刪除事項", "info");
      } catch (err) {
        showToast("刪除失敗：" + err.message, "error");
      }
    });
  });
}

function populateAssigneeDropdowns() {
  const activeList = state.lists.find((l) => l.id === state.activeListId);
  const members = activeList?.members || [];

  const newSelect = document.getElementById("new-task-assignee");
  const editSelect = document.getElementById("edit-task-assignee");

  const buildOptions = () => {
    let html = '<option value="">未指派</option>';
    members.forEach((m) => {
      const name = m.displayName || m.email;
      html += `<option value="${escapeHtml(m.email)}">${escapeHtml(name)}</option>`;
    });
    return html;
  };

  if (newSelect) newSelect.innerHTML = buildOptions();
  if (editSelect) editSelect.innerHTML = buildOptions();
}

// ==========================================
// Event Listeners & Modals
// ==========================================
function setupEventListeners() {
  // Mobile Sidebar Toggle
  const toggleBtn = document.getElementById("btn-toggle-sidebar");
  const sidebar = document.getElementById("sidebar");
  const backdrop = document.getElementById("sidebar-backdrop");

  if (toggleBtn && sidebar && backdrop) {
    toggleBtn.addEventListener("click", () => {
      sidebar.classList.toggle("-translate-x-full");
      backdrop.classList.toggle("hidden");
    });
    backdrop.addEventListener("click", closeMobileSidebar);
  }

  // Hero Google Login
  const heroLogin = document.getElementById("hero-btn-google-login");
  if (heroLogin) heroLogin.addEventListener("click", handleGoogleLogin);

  // Logout
  const btnLogout = document.getElementById("btn-logout");
  if (btnLogout) {
    btnLogout.addEventListener("click", async () => {
      if (confirm("確定要登出嗎？")) {
        await logoutUser();
        showToast("已安全登出", "info");
      }
    });
  }

  // Firebase Settings Modal triggers
  const btnOpenSettings = document.getElementById("btn-open-firebase-settings");
  const heroSettings = document.getElementById("hero-btn-settings");
  const bannerSettings = document.getElementById("banner-open-settings");

  [btnOpenSettings, heroSettings, bannerSettings].forEach((el) => {
    if (el) el.addEventListener("click", () => openModal("modal-firebase-settings"));
  });

  // Modal Close buttons
  document.querySelectorAll(".modal-close, .modal-backdrop").forEach((el) => {
    el.addEventListener("click", () => {
      closeAllModals();
    });
  });

  // Parse Firebase Config
  const btnParse = document.getElementById("btn-parse-config");
  if (btnParse) {
    btnParse.addEventListener("click", parsePastedConfig);
  }

  // Save Firebase Config
  const btnSaveConfig = document.getElementById("btn-save-firebase-config");
  if (btnSaveConfig) {
    btnSaveConfig.addEventListener("click", () => {
      const apiKey = document.getElementById("cfg-api-key").value.trim();
      const authDomain = document.getElementById("cfg-auth-domain").value.trim();
      const projectId = document.getElementById("cfg-project-id").value.trim();
      const appId = document.getElementById("cfg-app-id").value.trim();

      if (!apiKey) {
        alert("請至少填寫 API Key");
        return;
      }

      saveConfig({
        apiKey,
        authDomain,
        projectId,
        appId,
        storageBucket: `${projectId}.firebasestorage.app`
      });

      alert("Firebase 設定已儲存！頁面將自動重新載入套用。");
      window.location.reload();
    });
  }

  // Reset Config
  const btnResetConfig = document.getElementById("btn-reset-config");
  if (btnResetConfig) {
    btnResetConfig.addEventListener("click", () => {
      if (confirm("確定要重設為預設設定嗎？")) {
        resetConfig();
        window.location.reload();
      }
    });
  }

  // Create List trigger
  const btnCreateList = document.getElementById("btn-create-list-trigger");
  if (btnCreateList) {
    btnCreateList.addEventListener("click", () => openCreateListModal());
  }

  // Form: Create / Edit List
  const formList = document.getElementById("form-list");
  if (formList) {
    formList.addEventListener("submit", handleListFormSubmit);
  }

  // Emoji presets in list modal
  document.querySelectorAll(".emoji-preset").forEach((btn) => {
    btn.addEventListener("click", () => {
      const input = document.getElementById("modal-list-icon");
      if (input) input.value = btn.textContent.trim();
    });
  });

  // Color swatches in list modal
  document.querySelectorAll(".color-swatch").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".color-swatch").forEach((b) => b.classList.remove("ring-2", "ring-offset-2", "ring-indigo-500"));
      btn.classList.add("ring-2", "ring-offset-2", "ring-indigo-500");
      const hiddenInput = document.getElementById("modal-list-color");
      if (hiddenInput) hiddenInput.value = btn.getAttribute("data-color");
    });
  });

  // List Actions dropdown toggle
  const btnListActions = document.getElementById("btn-list-actions-menu");
  const listActionsMenu = document.getElementById("list-actions-dropdown");
  if (btnListActions && listActionsMenu) {
    btnListActions.addEventListener("click", (e) => {
      e.stopPropagation();
      listActionsMenu.classList.toggle("hidden");
    });
    document.addEventListener("click", () => {
      listActionsMenu.classList.add("hidden");
    });
  }

  // Edit active list
  const btnEditActiveList = document.getElementById("btn-edit-active-list");
  if (btnEditActiveList) {
    btnEditActiveList.addEventListener("click", () => {
      const list = state.lists.find((l) => l.id === state.activeListId);
      if (list) openEditListModal(list);
    });
  }

  // Delete active list
  const btnDeleteActiveList = document.getElementById("btn-delete-active-list");
  if (btnDeleteActiveList) {
    btnDeleteActiveList.addEventListener("click", async () => {
      const list = state.lists.find((l) => l.id === state.activeListId);
      if (!list) return;

      const isOwner = list.ownerEmail === state.currentUser?.email?.toLowerCase();
      if (!isOwner) {
        showToast("只有清單建立者才能刪除整張清單", "error");
        return;
      }

      if (!confirm(`確定要刪除「${list.title}」清單及其所有待辦事項嗎？此動作無法復原。`)) return;

      try {
        await deleteList(state.activeListId);
        showToast("清單已刪除", "info");
      } catch (err) {
        showToast("刪除清單失敗：" + err.message, "error");
      }
    });
  }

  // Share modal trigger
  const btnOpenShare = document.getElementById("btn-open-share-modal");
  const avatarGroup = document.getElementById("collaborators-avatar-group");
  [btnOpenShare, avatarGroup].forEach((el) => {
    if (el) {
      el.addEventListener("click", () => {
        const list = state.lists.find((l) => l.id === state.activeListId);
        if (list) openShareModal(list);
      });
    }
  });

  // Copy list link shortcut
  const btnCopyListLink = document.getElementById("btn-copy-list-link");
  if (btnCopyListLink) {
    btnCopyListLink.addEventListener("click", copyShareLink);
  }

  // Copy share link button in share modal
  const btnCopyShareLink = document.getElementById("btn-copy-share-link");
  if (btnCopyShareLink) {
    btnCopyShareLink.addEventListener("click", copyShareLink);
  }

  // Invite member by email form
  const formInvite = document.getElementById("form-invite-member");
  if (formInvite) {
    formInvite.addEventListener("submit", handleInviteMemberSubmit);
  }

  // Form: Create Task
  const formCreateTask = document.getElementById("create-task-form");
  if (formCreateTask) {
    formCreateTask.addEventListener("submit", handleCreateTaskSubmit);
  }

  // Form: Edit Task
  const formEditTask = document.getElementById("form-edit-task");
  if (formEditTask) {
    formEditTask.addEventListener("submit", handleEditTaskSubmit);
  }

  // Search Input
  const searchInput = document.getElementById("task-search-input");
  if (searchInput) {
    searchInput.addEventListener("input", (e) => {
      state.searchQuery = e.target.value;
      renderTodos();
    });
  }

  // Status Filter Pills (全部 / 未完成 / 已完成)
  document.querySelectorAll(".status-pill-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".status-pill-btn").forEach((b) => {
        b.classList.remove("active", "bg-white", "text-slate-800", "shadow-xs", "font-semibold");
        b.classList.add("hover:text-slate-900");
      });
      btn.classList.add("active", "bg-white", "text-slate-800", "shadow-xs", "font-semibold");
      btn.classList.remove("hover:text-slate-900");

      state.filterStatus = btn.getAttribute("data-status-filter");
      renderTodos();
    });
  });

  // Priority Filter Dropdown
  const priorityFilter = document.getElementById("priority-filter");
  if (priorityFilter) {
    priorityFilter.addEventListener("change", (e) => {
      state.filterPriority = e.target.value;
      renderTodos();
    });
  }

  // Sidebar Quick Filters
  document.querySelectorAll(".sidebar-filter-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".sidebar-filter-btn").forEach((b) => b.classList.remove("bg-slate-100", "font-bold"));
      btn.classList.add("bg-slate-100", "font-bold");

      state.quickFilter = btn.getAttribute("data-quick-filter");
      renderTodos();
      closeMobileSidebar();
    });
  });
}

function closeMobileSidebar() {
  const sidebar = document.getElementById("sidebar");
  const backdrop = document.getElementById("sidebar-backdrop");
  if (sidebar) sidebar.classList.add("-translate-x-full");
  if (backdrop) backdrop.classList.add("hidden");
}

// ==========================================
// Modal Operations
// ==========================================
function openModal(id) {
  const modal = document.getElementById(id);
  if (modal) {
    modal.classList.remove("hidden");
    lucide.createIcons({ root: modal });
  }
}

function closeAllModals() {
  document.querySelectorAll('[id^="modal-"]').forEach((m) => m.classList.add("hidden"));
}

function openCreateListModal() {
  document.getElementById("modal-list-title").textContent = "建立新協作清單";
  document.getElementById("modal-list-id").value = "";
  document.getElementById("modal-list-name").value = "";
  document.getElementById("modal-list-description").value = "";
  document.getElementById("modal-list-icon").value = "📋";
  document.getElementById("modal-list-color").value = "#6366f1";
  openModal("modal-list");
}

function openEditListModal(list) {
  document.getElementById("modal-list-title").textContent = "編輯清單資訊";
  document.getElementById("modal-list-id").value = list.id;
  document.getElementById("modal-list-name").value = list.title;
  document.getElementById("modal-list-description").value = list.description || "";
  document.getElementById("modal-list-icon").value = list.icon || "📋";
  document.getElementById("modal-list-color").value = list.color || "#6366f1";
  openModal("modal-list");
}

async function handleListFormSubmit(e) {
  e.preventDefault();
  const id = document.getElementById("modal-list-id").value;
  const title = document.getElementById("modal-list-name").value.trim();
  const description = document.getElementById("modal-list-description").value.trim();
  const icon = document.getElementById("modal-list-icon").value.trim() || "📋";
  const color = document.getElementById("modal-list-color").value || "#6366f1";

  if (!title) return;

  try {
    if (id) {
      await updateList(id, { title, description, icon, color });
      showToast("清單資訊已更新", "success");
    } else {
      const created = await createList({
        title,
        description,
        icon,
        color,
        currentUser: state.currentUser
      });
      showToast("清單建立成功！", "success");
      selectList(created.id);
    }
    closeAllModals();
  } catch (err) {
    showToast("儲存清單失敗：" + err.message, "error");
  }
}

function openShareModal(list) {
  document.getElementById("share-modal-list-title").textContent = `清單：${list.title}`;

  // Populate invite link
  const linkInput = document.getElementById("share-link-input");
  if (linkInput) {
    const inviteUrl = `${window.location.origin}${window.location.pathname}?join=${list.id}`;
    linkInput.value = inviteUrl;
  }

  // Populate member list
  const container = document.getElementById("members-list-container");
  if (container) {
    const members = list.members || [];
    const isOwner = list.ownerEmail === state.currentUser?.email?.toLowerCase();

    container.innerHTML = members
      .map((m) => {
        const isSelf = m.email.toLowerCase() === state.currentUser?.email?.toLowerCase();
        const isMemberOwner = m.role === "owner" || m.email.toLowerCase() === list.ownerEmail.toLowerCase();

        return `
          <div class="p-3 flex items-center justify-between bg-white text-xs">
            <div class="flex items-center space-x-2.5 min-w-0">
              <img class="w-7 h-7 rounded-full border border-slate-200" src="${m.photoURL || `https://ui-avatars.com/api/?name=${encodeURIComponent(m.displayName || m.email)}&background=6366f1&color=fff`}">
              <div class="truncate">
                <p class="font-semibold text-slate-800 truncate">${escapeHtml(m.displayName || m.email)} ${isSelf ? '<span class="text-slate-400 font-normal">(您)</span>' : ''}</p>
                <p class="text-[11px] text-slate-400 truncate">${escapeHtml(m.email)}</p>
              </div>
            </div>
            <div class="flex items-center space-x-2">
              <span class="px-2 py-0.5 rounded-full text-[10px] font-semibold ${isMemberOwner ? 'bg-amber-50 text-amber-700 border border-amber-200' : 'bg-slate-100 text-slate-600'}">
                ${isMemberOwner ? '建立者' : '協作者'}
              </span>
              ${
                isOwner && !isMemberOwner
                  ? `<button data-remove-email="${escapeHtml(m.email)}" class="btn-remove-member p-1 text-slate-400 hover:text-rose-600 hover:bg-rose-50 rounded-md transition-colors" title="移除成員">
                      <i data-lucide="user-x" class="w-3.5 h-3.5"></i>
                    </button>`
                  : ''
              }
            </div>
          </div>
        `;
      })
      .join("");

    // Attach remove member events
    container.querySelectorAll(".btn-remove-member").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const email = btn.getAttribute("data-remove-email");
        if (!confirm(`確定要將 ${email} 從這張協作清單移除嗎？`)) return;

        try {
          await removeCollaborator(state.activeListId, email);
          showToast(`已移除協作者 ${email}`, "info");
          // Refresh share modal with updated list
          const updatedList = await getListById(state.activeListId);
          if (updatedList) openShareModal(updatedList);
        } catch (err) {
          showToast("移除成員失敗：" + err.message, "error");
        }
      });
    });

    lucide.createIcons({ root: container });
  }

  openModal("modal-share");
}

async function handleInviteMemberSubmit(e) {
  e.preventDefault();
  const emailInput = document.getElementById("invite-email-input");
  const email = emailInput.value.trim();
  if (!email) return;

  try {
    await addCollaborator(state.activeListId, email);
    showToast(`已成功邀請 ${email}！`, "success");
    emailInput.value = "";
    const updatedList = await getListById(state.activeListId);
    if (updatedList) openShareModal(updatedList);
  } catch (err) {
    showToast(err.message || "邀請失敗", "error");
  }
}

function copyShareLink() {
  const linkInput = document.getElementById("share-link-input");
  const inviteUrl = linkInput?.value || `${window.location.origin}${window.location.pathname}?join=${state.activeListId}`;

  navigator.clipboard.writeText(inviteUrl).then(() => {
    showToast("協作邀請連結已複製到剪貼簿！", "success");
  }).catch(() => {
    showToast("複製失敗，請手動複製網址", "warning");
  });
}

// ==========================================
// Task Management
// ==========================================
async function handleCreateTaskSubmit(e) {
  e.preventDefault();
  if (!state.activeListId) {
    showToast("請先選擇或建立清單", "warning");
    return;
  }

  const titleInput = document.getElementById("new-task-title");
  const prioritySelect = document.getElementById("new-task-priority");
  const dueDateInput = document.getElementById("new-task-duedate");
  const assigneeSelect = document.getElementById("new-task-assignee");

  const title = titleInput.value.trim();
  if (!title) return;

  const priority = prioritySelect.value;
  const dueDate = dueDateInput.value || null;

  // Selected assignee
  let assignedTo = null;
  if (assigneeSelect.value) {
    const activeList = state.lists.find((l) => l.id === state.activeListId);
    const member = (activeList?.members || []).find((m) => m.email.toLowerCase() === assigneeSelect.value.toLowerCase());
    if (member) {
      assignedTo = {
        email: member.email,
        displayName: member.displayName || member.email,
        photoURL: member.photoURL || ""
      };
    }
  }

  try {
    await createTodo(state.activeListId, {
      title,
      priority,
      dueDate,
      assignedTo,
      currentUser: state.currentUser
    });

    titleInput.value = "";
    dueDateInput.value = "";
    assigneeSelect.value = "";
    prioritySelect.value = "medium";
    titleInput.focus();
    showToast("已建立新事項", "success");
  } catch (err) {
    showToast("建立事項失敗：" + err.message, "error");
  }
}

function openEditTaskModal(todo) {
  document.getElementById("edit-task-id").value = todo.id;
  document.getElementById("edit-task-title").value = todo.title;
  document.getElementById("edit-task-notes").value = todo.notes || "";
  document.getElementById("edit-task-priority").value = todo.priority || "medium";
  document.getElementById("edit-task-duedate").value = todo.dueDate || "";
  document.getElementById("edit-task-assignee").value = todo.assignedTo?.email || "";

  openModal("modal-task-edit");
}

async function handleEditTaskSubmit(e) {
  e.preventDefault();
  const todoId = document.getElementById("edit-task-id").value;
  const title = document.getElementById("edit-task-title").value.trim();
  const notes = document.getElementById("edit-task-notes").value.trim();
  const priority = document.getElementById("edit-task-priority").value;
  const dueDate = document.getElementById("edit-task-duedate").value || null;
  const assigneeEmail = document.getElementById("edit-task-assignee").value;

  if (!title) return;

  let assignedTo = null;
  if (assigneeEmail) {
    const activeList = state.lists.find((l) => l.id === state.activeListId);
    const member = (activeList?.members || []).find((m) => m.email.toLowerCase() === assigneeEmail.toLowerCase());
    if (member) {
      assignedTo = {
        email: member.email,
        displayName: member.displayName || member.email,
        photoURL: member.photoURL || ""
      };
    }
  }

  try {
    await updateTodo(state.activeListId, todoId, {
      title,
      notes,
      priority,
      dueDate,
      assignedTo
    });

    showToast("事項已更新", "success");
    closeAllModals();
  } catch (err) {
    showToast("更新事項失敗：" + err.message, "error");
  }
}

// ==========================================
// Config Parser Helper
// ==========================================
function parsePastedConfig() {
  const text = document.getElementById("firebase-quick-paste").value;
  if (!text.trim()) return;

  try {
    const apiKeyMatch = text.match(/apiKey:\s*["']([^"']+)["']/i);
    const authDomainMatch = text.match(/authDomain:\s*["']([^"']+)["']/i);
    const projectIdMatch = text.match(/projectId:\s*["']([^"']+)["']/i);
    const appIdMatch = text.match(/appId:\s*["']([^"']+)["']/i);

    if (apiKeyMatch) document.getElementById("cfg-api-key").value = apiKeyMatch[1];
    if (authDomainMatch) document.getElementById("cfg-auth-domain").value = authDomainMatch[1];
    if (projectIdMatch) document.getElementById("cfg-project-id").value = projectIdMatch[1];
    if (appIdMatch) document.getElementById("cfg-app-id").value = appIdMatch[1];

    if (!apiKeyMatch) {
      // Try JSON parse
      const obj = JSON.parse(text);
      if (obj.apiKey) document.getElementById("cfg-api-key").value = obj.apiKey;
      if (obj.authDomain) document.getElementById("cfg-auth-domain").value = obj.authDomain;
      if (obj.projectId) document.getElementById("cfg-project-id").value = obj.projectId;
      if (obj.appId) document.getElementById("cfg-app-id").value = obj.appId;
    }

    showToast("已成功解析 Firebase 設定！請點擊下方儲存。", "success");
  } catch (err) {
    showToast("解析失敗，請確認格式或直接填入下方各欄位", "error");
  }
}
