// Firebase Configuration and Initialization
// Project: my-to-do-list-ac553
import { initializeApp } from "https://www.gstatic.com/firebasejs/10.12.0/firebase-app.js";
import { getAuth, GoogleAuthProvider } from "https://www.gstatic.com/firebasejs/10.12.0/firebase-auth.js";
import { getFirestore } from "https://www.gstatic.com/firebasejs/10.12.0/firebase-firestore.js";

// Production Firebase Configuration
export const firebaseConfig = {
  apiKey: "AIzaSyC5ujYnqrRacsXXQf-F8VlslnhQOqBEyfw",
  authDomain: "my-to-do-list-ac553.firebaseapp.com",
  projectId: "my-to-do-list-ac553",
  storageBucket: "my-to-do-list-ac553.firebasestorage.app",
  messagingSenderId: "792218317942",
  appId: "1:792218317942:web:2100cc29e5e0c135d5db57",
  measurementId: "G-QZ4311DNT7"
};

export function isConfigComplete() {
  return true;
}

// Initialize Firebase App & Services
export const app = initializeApp(firebaseConfig);
export const auth = getAuth(app);
export const db = getFirestore(app);

export const googleProvider = new GoogleAuthProvider();
googleProvider.setCustomParameters({ prompt: 'select_account' });
