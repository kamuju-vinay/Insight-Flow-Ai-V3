import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App.jsx";

class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null, info: null };
  }
  componentDidCatch(error, info) {
    this.setState({ error, info });
    console.error("React Error Boundary caught:", error, info);
  }
  render() {
    if (this.state.error) {
      return (
        <div style={{
          padding: 32, fontFamily: "monospace", background: "#fef2f2",
          minHeight: "100vh", color: "#7f1d1d",
        }}>
          <h2 style={{ color: "#dc2626", marginBottom: 16 }}>⚠️ App crashed — see error below</h2>
          <pre style={{
            background: "#fff", border: "1px solid #fca5a5", borderRadius: 8,
            padding: 16, whiteSpace: "pre-wrap", wordBreak: "break-word",
            fontSize: 13, color: "#1e1e1e", maxHeight: "60vh", overflowY: "auto",
          }}>
            {this.state.error?.toString()}
            {"\n\n"}
            {this.state.info?.componentStack}
          </pre>
          <button
            onClick={() => window.location.reload()}
            style={{
              marginTop: 16, padding: "8px 20px", background: "#dc2626",
              color: "#fff", border: "none", borderRadius: 6, cursor: "pointer", fontSize: 14,
            }}
          >
            Reload
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>
);
