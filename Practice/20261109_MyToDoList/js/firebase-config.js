// Firebase Configuration and Initialization
// Existing Firebase Project: my-to-do-list-ac553

import { initializeApp } from "https://www.gstatic.com/firebasejs/10.12.0/firebase-app.js";
import { getAuth, GoogleAuthProvider } from "https://www.gstatic.com/firebasejs/10.12.0/firebase-auth.js";
import { getFirestore } from "https://www.gstatic.com/firebasejs/10.12.0/firebase-firestore.js";

// Default configuration with your existing project ID
const DEFAULT_CONFIG = {
  apiKey: "", // Fill your web API key here or via the in-app Settings modal
  authDomain: "my-to-do-list-ac553.firebaseapp.com",
  projectId: "my-to-do-list-ac553",
  storageBucket: "my-to-do-list-ac553.firebasestorage.app",
  messagingSenderId: "",
  appId: ""
};

const STORAGE_KEY = "my_todo_firebase_config_v1";

// Load config: prioritize localStorage if user entered credentials, otherwise fallback to DEFAULT_CONFIG
export function getStoredConfig() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved) {
      const parsed = JSON.parse(saved);
      return { ...DEFAULT_CONFIG, ...parsed, projectId: "my-to-do-list-ac553" };
    }
  } catch (err) {
    console.warn("Failed to read Firebase config from storage:", err);
  }
  return { ...DEFAULT_CONFIG };
}

export function saveConfig(newConfig) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(newConfig));
    return true;
  } catch (err) {
    console.error("Failed to save Firebase config:", err);
    return false;
  }
}

export function resetConfig() {
  localStorage.removeItem(STORAGE_KEY);
}

export function isConfigComplete(config = getStoredConfig()) {
  return Boolean(config && config.apiKey && config.apiKey.trim().length > 10 && config.projectId);
}

// Current active configuration
export const activeConfig = getStoredConfig();

let app = null;
let auth = null;
let db = null;
let googleProvider = null;
let initError = null;

try {
  if (isConfigComplete(activeConfig)) {
    app = initializeApp(activeConfig);
    auth = getAuth(app);
    db = getFirestore(app);
    googleProvider = new GoogleAuthProvider();
    googleProvider.setCustomParameters({ prompt: 'select_account' });
  } else {
    console.info("Firebase: apiKey 尚未填入，請點擊右上角「Firebase 設定」填入 Web App 資訊。");
  }
} catch (err) {
  console.error("Firebase init error:", err);
  initError = err;
}

export { app, auth, db, googleProvider, initError };
