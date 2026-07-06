// LoginPage.jsx — Login and signup UI
import { useState } from "react";
import { Brain, Mail, Lock, LogIn, UserPlus, AlertTriangle } from "lucide-react";
import { apiLogin, apiSignup, setToken } from "./api.js";

const C = {
  ink: "#0f0e0d", ink2: "#44433f", ink3: "#888780",
  paper: "#fafaf8", surface: "#f3f2ee", surface2: "#eae9e3",
  line: "rgba(0,0,0,.08)", line2: "rgba(0,0,0,.14)",
  accent: "#2f54eb", accentBg: "#eef2ff",
  red: "#dc2626", redBg: "#fef2f2",
  green: "#16a34a",
};

export default function LoginPage({ onAuth }) {
  const [mode, setMode] = useState("login"); // 'login' | 'signup'
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const submit = async () => {
    setError("");
    if (!email.trim() || !password) {
      setError("Email and password are required.");
      return;
    }
    if (mode === "signup" && password !== confirm) {
      setError("Passwords do not match.");
      return;
    }
    if (mode === "signup" && password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }

    setLoading(true);
    try {
      const fn = mode === "login" ? apiLogin : apiSignup;
      const data = await fn(email.trim(), password);
      if (data.error) {
        setError(data.error);
        return;
      }
      if (data.token) {
        setToken(data.token);
        localStorage.setItem("insightflow_token", data.token);
        onAuth(data.user);
      }
    } catch (e) {
      setError(e.message || "Network error. Please try again.");
    } finally {
      setLoading(false);
    }
  };

  const onKey = (e) => { if (e.key === "Enter") submit(); };

  return (
    <div style={{
      display: "flex", alignItems: "center", justifyContent: "center",
      minHeight: "100vh", background: C.surface,
      fontFamily: "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif",
    }}>
      <div style={{
        width: 400, background: "#fff", border: `1px solid ${C.line2}`,
        borderRadius: 16, padding: 32, boxShadow: "0 4px 32px rgba(0,0,0,.08)",
      }}>
        {/* Logo */}
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 28 }}>
          <img src="/logo.png" alt="Logo" style={{ width: 36, height: 36, borderRadius: 8, objectFit: "cover" }} />
          <div>
            <div style={{ fontSize: 15, fontWeight: 800, color: C.ink }}>Insight Flow AI</div>
            <div style={{ fontSize: 11, color: C.ink3 }}>Article monitoring & AI digest</div>
          </div>
        </div>

        {/* Mode toggle */}
        <div style={{ display: "flex", gap: 4, marginBottom: 24, background: C.surface, borderRadius: 10, padding: 4 }}>
          {["login", "signup"].map(m => (
            <button key={m} onClick={() => { setMode(m); setError(""); }}
              style={{
                flex: 1, padding: "7px 0", borderRadius: 7, border: "none",
                cursor: "pointer", fontSize: 13, fontWeight: 600,
                fontFamily: "inherit",
                background: mode === m ? "#fff" : "transparent",
                color: mode === m ? C.ink : C.ink3,
                boxShadow: mode === m ? "0 1px 4px rgba(0,0,0,.1)" : "none",
                transition: "all .15s",
              }}>
              {m === "login" ? "Log In" : "Sign Up"}
            </button>
          ))}
        </div>

        {/* Fields */}
        <div style={{ marginBottom: 14 }}>
          <label style={{ fontSize: 11, fontWeight: 600, color: C.ink2, marginBottom: 5, display: "flex", alignItems: "center", gap: 5 }}>
            <Mail size={12} /> Email address
          </label>
          <input
            type="email" value={email} onChange={e => setEmail(e.target.value)}
            onKeyDown={onKey}
            placeholder="you@example.com"
            style={{
              width: "100%", padding: "9px 12px", border: `1px solid ${C.line2}`,
              borderRadius: 8, fontSize: 13, color: C.ink, background: "#fff",
              outline: "none", fontFamily: "inherit", boxSizing: "border-box",
            }}
          />
        </div>

        <div style={{ marginBottom: mode === "signup" ? 14 : 20 }}>
          <label style={{ fontSize: 11, fontWeight: 600, color: C.ink2, marginBottom: 5, display: "flex", alignItems: "center", gap: 5 }}>
            <Lock size={12} /> Password
          </label>
          <input
            type="password" value={password} onChange={e => setPassword(e.target.value)}
            onKeyDown={onKey}
            placeholder={mode === "signup" ? "Min 8 characters" : "••••••••"}
            style={{
              width: "100%", padding: "9px 12px", border: `1px solid ${C.line2}`,
              borderRadius: 8, fontSize: 13, color: C.ink, background: "#fff",
              outline: "none", fontFamily: "inherit", boxSizing: "border-box",
            }}
          />
        </div>

        {mode === "signup" && (
          <div style={{ marginBottom: 20 }}>
            <label style={{ fontSize: 11, fontWeight: 600, color: C.ink2, marginBottom: 5, display: "flex", alignItems: "center", gap: 5 }}>
              <Lock size={12} /> Confirm Password
            </label>
            <input
              type="password" value={confirm} onChange={e => setConfirm(e.target.value)}
              onKeyDown={onKey}
              placeholder="Re-enter password"
              style={{
                width: "100%", padding: "9px 12px", border: `1px solid ${C.line2}`,
                borderRadius: 8, fontSize: 13, color: C.ink, background: "#fff",
                outline: "none", fontFamily: "inherit", boxSizing: "border-box",
              }}
            />
          </div>
        )}

        {error && (
          <div style={{
            display: "flex", alignItems: "center", gap: 8, padding: "9px 12px",
            background: C.redBg, border: `1px solid rgba(220,38,38,.2)`,
            borderRadius: 8, marginBottom: 16,
          }}>
            <AlertTriangle size={14} color={C.red} />
            <span style={{ fontSize: 12, color: C.red }}>{error}</span>
          </div>
        )}

        <button
          onClick={submit}
          disabled={loading}
          style={{
            width: "100%", padding: "10px 0", borderRadius: 9,
            background: C.accent, color: "#fff", border: "none",
            fontSize: 14, fontWeight: 700, cursor: loading ? "wait" : "pointer",
            fontFamily: "inherit", display: "flex", alignItems: "center",
            justifyContent: "center", gap: 8, opacity: loading ? 0.7 : 1,
            transition: "opacity .15s",
          }}
        >
          {mode === "login"
            ? <><LogIn size={15} /> {loading ? "Signing in…" : "Sign In"}</>
            : <><UserPlus size={15} /> {loading ? "Creating account…" : "Create Account"}</>
          }
        </button>

        <div style={{ marginTop: 20, fontSize: 11, color: C.ink3, textAlign: "center", lineHeight: 1.6 }}>
          {mode === "login"
            ? <>Don&apos;t have an account? <button onClick={() => { setMode("signup"); setError(""); }} style={{ background: "none", border: "none", color: C.accent, cursor: "pointer", fontSize: 11, fontFamily: "inherit", fontWeight: 600 }}>Sign up</button></>
            : <>Already have an account? <button onClick={() => { setMode("login"); setError(""); }} style={{ background: "none", border: "none", color: C.accent, cursor: "pointer", fontSize: 11, fontFamily: "inherit", fontWeight: 600 }}>Sign in</button></>
          }
        </div>
      </div>
    </div>
  );
}
