// Authentication Module (Google Sign-In)
import { 
  signInWithPopup, 
  signOut, 
  onAuthStateChanged 
} from "https://www.gstatic.com/firebasejs/10.12.0/firebase-auth.js";
import { auth, googleProvider, isConfigComplete } from "./firebase-config.js";

/**
 * Perform Google Sign-in with popup
 */
export async function loginWithGoogle() {
  if (!isConfigComplete() || !auth || !googleProvider) {
    throw new Error("請先在右上角設定 Firebase API Key 才能啟用 Google 登入功能。");
  }

  try {
    const result = await signInWithPopup(auth, googleProvider);
    const user = result.user;
    return {
      uid: user.uid,
      displayName: user.displayName || "使用者",
      email: user.email,
      photoURL: user.photoURL || `https://ui-avatars.com/api/?name=${encodeURIComponent(user.displayName || "User")}&background=6366f1&color=fff`
    };
  } catch (error) {
    console.error("Google 登入失敗:", error);
    let friendlyMessage = "登入失敗，請稍後再試。";
    if (error.code === "auth/popup-closed-by-user") {
      friendlyMessage = "登入視窗已被關閉。";
    } else if (error.code === "auth/unauthorized-domain") {
      friendlyMessage = "此網域尚未加入 Firebase Authentication 的已授權網域 (Authorized Domains)。請至 Firebase 控制台 > Authentication > Settings 加入 localhost。";
    } else if (error.code === "auth/operation-not-allowed") {
      friendlyMessage = "Firebase 專案尚未啟用 Google 登入提供者。請至 Firebase 控制台 > Authentication > Sign-in method 啟用 Google。";
    } else if (error.code === "auth/api-key-not-valid.invalid-api-key" || error.code === "auth/invalid-api-key") {
      friendlyMessage = "Firebase API Key 無效，請檢查設定。";
    } else if (error.message) {
      friendlyMessage = error.message;
    }
    const err = new Error(friendlyMessage);
    err.original = error;
    throw err;
  }
}

/**
 * Sign out current user
 */
export async function logoutUser() {
  if (!auth) return;
  try {
    await signOut(auth);
  } catch (error) {
    console.error("登出失敗:", error);
    throw error;
  }
}

/**
 * Listen to Auth State Changes
 */
export function subscribeAuth(onUserChanged) {
  if (!auth) {
    onUserChanged(null);
    return () => {};
  }
  return onAuthStateChanged(auth, (user) => {
    if (user) {
      onUserChanged({
        uid: user.uid,
        displayName: user.displayName || "使用者",
        email: user.email,
        photoURL: user.photoURL || `https://ui-avatars.com/api/?name=${encodeURIComponent(user.displayName || "User")}&background=6366f1&color=fff`
      });
    } else {
      onUserChanged(null);
    }
  });
}

/**
 * Get current authenticated user
 */
export function getCurrentUser() {
  if (!auth || !auth.currentUser) return null;
  const user = auth.currentUser;
  return {
    uid: user.uid,
    displayName: user.displayName || "使用者",
    email: user.email,
    photoURL: user.photoURL || `https://ui-avatars.com/api/?name=${encodeURIComponent(user.displayName || "User")}&background=6366f1&color=fff`
  };
}
