// Database Module (Cloud Firestore Real-time Collaboration)
import {
  collection,
  doc,
  addDoc,
  updateDoc,
  deleteDoc,
  getDoc,
  onSnapshot,
  query,
  where,
  orderBy,
  serverTimestamp,
  arrayUnion,
  arrayRemove
} from "https://www.gstatic.com/firebasejs/10.12.0/firebase-firestore.js";
import { db } from "./firebase-config.js";

/**
 * Subscribe to all lists where the user is an owner or collaborator
 */
export function subscribeUserLists(userEmail, callback, onError) {
  if (!db || !userEmail) {
    callback([]);
    return () => {};
  }

  const normalizedEmail = userEmail.toLowerCase().trim();
  const listsRef = collection(db, "lists");
  const q = query(listsRef, where("memberEmails", "array-contains", normalizedEmail));

  return onSnapshot(
    q,
    (snapshot) => {
      const lists = [];
      snapshot.forEach((docSnap) => {
        lists.push({ id: docSnap.id, ...docSnap.data() });
      });

      // Sort client-side by createdAt descending to avoid composite index requirement
      lists.sort((a, b) => {
        const timeA = a.createdAt?.toMillis ? a.createdAt.toMillis() : (a.createdAt || 0);
        const timeB = b.createdAt?.toMillis ? b.createdAt.toMillis() : (b.createdAt || 0);
        return timeB - timeA;
      });

      callback(lists);
    },
    (error) => {
      console.error("載入協作清單錯誤:", error);
      if (onError) onError(error);
    }
  );
}

/**
 * Create a new collaborative list
 */
export async function createList({ title, description = "", color = "#6366f1", icon = "📋", currentUser }) {
  if (!db) throw new Error("資料庫未初始化");
  if (!currentUser?.email) throw new Error("需要登入才能建立清單");

  const normalizedEmail = currentUser.email.toLowerCase().trim();
  const listsRef = collection(db, "lists");

  const newList = {
    title: title.trim(),
    description: description.trim(),
    color: color || "#6366f1",
    icon: icon || "📋",
    ownerUid: currentUser.uid,
    ownerEmail: normalizedEmail,
    ownerName: currentUser.displayName || "未知使用者",
    ownerPhoto: currentUser.photoURL || "",
    memberEmails: [normalizedEmail],
    members: [
      {
        uid: currentUser.uid,
        email: normalizedEmail,
        displayName: currentUser.displayName || "使用者",
        photoURL: currentUser.photoURL || "",
        role: "owner"
      }
    ],
    createdAt: serverTimestamp(),
    updatedAt: serverTimestamp()
  };

  const docRef = await addDoc(listsRef, newList);
  return { id: docRef.id, ...newList };
}

/**
 * Update list details
 */
export async function updateList(listId, updates) {
  if (!db) throw new Error("資料庫未初始化");
  const docRef = doc(db, "lists", listId);
  await updateDoc(docRef, {
    ...updates,
    updatedAt: serverTimestamp()
  });
}

/**
 * Delete a list
 */
export async function deleteList(listId) {
  if (!db) throw new Error("資料庫未初始化");
  const docRef = doc(db, "lists", listId);
  await deleteDoc(docRef);
}

/**
 * Get single list by ID
 */
export async function getListById(listId) {
  if (!db) throw new Error("資料庫未初始化");
  const docRef = doc(db, "lists", listId);
  const snap = await getDoc(docRef);
  if (!snap.exists()) return null;
  return { id: snap.id, ...snap.data() };
}

/**
 * Add collaborator to list by email
 */
export async function addCollaborator(listId, email, role = "editor") {
  if (!db) throw new Error("資料庫未初始化");
  const normalizedEmail = email.toLowerCase().trim();
  const docRef = doc(db, "lists", listId);

  // Fetch current list to check duplicate
  const snap = await getDoc(docRef);
  if (!snap.exists()) throw new Error("找不到指定的待辦清單");

  const data = snap.data();
  if (data.memberEmails && data.memberEmails.includes(normalizedEmail)) {
    throw new Error("此成員已在清單中");
  }

  const newMemberObj = {
    uid: "",
    email: normalizedEmail,
    displayName: normalizedEmail.split("@")[0],
    photoURL: `https://ui-avatars.com/api/?name=${encodeURIComponent(normalizedEmail.split("@")[0])}&background=10b981&color=fff`,
    role: role
  };

  await updateDoc(docRef, {
    memberEmails: arrayUnion(normalizedEmail),
    members: arrayUnion(newMemberObj),
    updatedAt: serverTimestamp()
  });

  return newMemberObj;
}

/**
 * Remove collaborator from list
 */
export async function removeCollaborator(listId, email) {
  if (!db) throw new Error("資料庫未初始化");
  const normalizedEmail = email.toLowerCase().trim();
  const docRef = doc(db, "lists", listId);

  const snap = await getDoc(docRef);
  if (!snap.exists()) return;

  const data = snap.data();
  if (data.ownerEmail === normalizedEmail) {
    throw new Error("不能移除清單擁有者");
  }

  const updatedMembers = (data.members || []).filter((m) => m.email.toLowerCase() !== normalizedEmail);
  const updatedEmails = (data.memberEmails || []).filter((e) => e.toLowerCase() !== normalizedEmail);

  await updateDoc(docRef, {
    memberEmails: updatedEmails,
    members: updatedMembers,
    updatedAt: serverTimestamp()
  });
}

/**
 * Join list using invite link
 */
export async function joinList(listId, currentUser) {
  if (!db) throw new Error("資料庫未初始化");
  if (!currentUser?.email) throw new Error("需登入才能加入協作");

  const normalizedEmail = currentUser.email.toLowerCase().trim();
  const docRef = doc(db, "lists", listId);
  const snap = await getDoc(docRef);

  if (!snap.exists()) {
    throw new Error("邀請連結無效或該清單已被刪除");
  }

  const data = snap.data();
  if (data.memberEmails && data.memberEmails.includes(normalizedEmail)) {
    return { alreadyJoined: true, list: { id: snap.id, ...data } };
  }

  const newMemberObj = {
    uid: currentUser.uid,
    email: normalizedEmail,
    displayName: currentUser.displayName || normalizedEmail.split("@")[0],
    photoURL: currentUser.photoURL || "",
    role: "editor"
  };

  await updateDoc(docRef, {
    memberEmails: arrayUnion(normalizedEmail),
    members: arrayUnion(newMemberObj),
    updatedAt: serverTimestamp()
  });

  return { alreadyJoined: false, list: { id: snap.id, ...data } };
}

// ==========================================
// Todos Real-time CRUD
// ==========================================

/**
 * Subscribe to todos of a specific list in real-time
 */
export function subscribeListTodos(listId, callback, onError) {
  if (!db || !listId) {
    callback([]);
    return () => {};
  }

  const todosRef = collection(db, "lists", listId, "todos");

  return onSnapshot(
    todosRef,
    (snapshot) => {
      const todos = [];
      snapshot.forEach((docSnap) => {
        todos.push({ id: docSnap.id, ...docSnap.data() });
      });

      // Sort client-side: incomplete first, then created desc
      todos.sort((a, b) => {
        if (a.completed !== b.completed) {
          return a.completed ? 1 : -1;
        }
        const timeA = a.createdAt?.toMillis ? a.createdAt.toMillis() : (a.createdAt || 0);
        const timeB = b.createdAt?.toMillis ? b.createdAt.toMillis() : (b.createdAt || 0);
        return timeB - timeA;
      });

      callback(todos);
    },
    (error) => {
      console.error("載入待辦事項錯誤:", error);
      if (onError) onError(error);
    }
  );
}

/**
 * Create a new task item
 */
export async function createTodo(listId, { title, priority = "medium", dueDate = null, assignedTo = null, notes = "", currentUser }) {
  if (!db) throw new Error("資料庫未初始化");
  if (!listId) throw new Error("未指定清單");

  const todosRef = collection(db, "lists", listId, "todos");
  const newTodo = {
    title: title.trim(),
    completed: false,
    priority: priority || "medium",
    dueDate: dueDate || null,
    assignedTo: assignedTo || null,
    notes: notes || "",
    createdBy: {
      uid: currentUser.uid,
      email: currentUser.email,
      displayName: currentUser.displayName || "使用者",
      photoURL: currentUser.photoURL || ""
    },
    createdAt: serverTimestamp(),
    completedAt: null
  };

  const docRef = await addDoc(todosRef, newTodo);
  return { id: docRef.id, ...newTodo };
}

/**
 * Toggle task completed status
 */
export async function toggleTodoStatus(listId, todoId, currentCompleted) {
  if (!db) throw new Error("資料庫未初始化");
  const docRef = doc(db, "lists", listId, "todos", todoId);
  await updateDoc(docRef, {
    completed: !currentCompleted,
    completedAt: !currentCompleted ? serverTimestamp() : null
  });
}

/**
 * Update todo item
 */
export async function updateTodo(listId, todoId, updates) {
  if (!db) throw new Error("資料庫未初始化");
  const docRef = doc(db, "lists", listId, "todos", todoId);
  await updateDoc(docRef, {
    ...updates,
    updatedAt: serverTimestamp()
  });
}

/**
 * Delete todo item
 */
export async function deleteTodo(listId, todoId) {
  if (!db) throw new Error("資料庫未初始化");
  const docRef = doc(db, "lists", listId, "todos", todoId);
  await deleteDoc(docRef);
}
