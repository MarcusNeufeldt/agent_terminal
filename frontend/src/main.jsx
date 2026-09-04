import React from "react";
import ReactDOM from "react-dom/client";
import "./styles.css";
import App from "./App";


import useStore from './store';
window.__store = useStore; // debug handle
ReactDOM.createRoot(document.getElementById("root")).render(<App />);
