import { createRoot } from "react-dom/client";
import App from "./App";
import { applyLayout, detectLayout } from "./layout";
import "./styles.css";

applyLayout(detectLayout());

createRoot(document.getElementById("root")!).render(<App />);
