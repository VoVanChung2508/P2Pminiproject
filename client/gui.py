import tkinter as tk
from tkinter import ttk

from client.client import P2PClient


class ClientGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("P2P Client")
        self.root.geometry("480x360")

        self.name_var = tk.StringVar(value="peer_1")
        self.host_var = tk.StringVar(value="192.168.56.2")
        self.port_var = tk.StringVar(value="8888")

        tk.Label(self.root, text="Tên peer").grid(row=0, column=0, padx=10, pady=10, sticky="w")
        tk.Entry(self.root, textvariable=self.name_var).grid(row=0, column=1, padx=10, pady=10, sticky="ew")

        tk.Label(self.root, text="Host").grid(row=1, column=0, padx=10, pady=10, sticky="w")
        tk.Entry(self.root, textvariable=self.host_var).grid(row=1, column=1, padx=10, pady=10, sticky="ew")

        tk.Label(self.root, text="Port").grid(row=2, column=0, padx=10, pady=10, sticky="w")
        tk.Entry(self.root, textvariable=self.port_var).grid(row=2, column=1, padx=10, pady=10, sticky="ew")

        ttk.Button(self.root, text="Đăng ký", command=self.register_peer).grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=8)
        ttk.Button(self.root, text="Xem peers", command=self.list_peers).grid(row=4, column=0, columnspan=2, sticky="ew", padx=10, pady=8)

        self.output = tk.Text(self.root, height=12)
        self.output.grid(row=5, column=0, columnspan=2, padx=10, pady=10, sticky="nsew")

        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(5, weight=1)

    def register_peer(self):
        client = P2PClient(self.name_var.get(), host=self.host_var.get(), tcp_port=int(self.port_var.get()))
        result = client.register()
        self.output.insert(tk.END, f"REGISTER: {result}\n")
        self.output.see(tk.END)

    def list_peers(self):
        client = P2PClient(self.name_var.get(), host=self.host_var.get(), tcp_port=int(self.port_var.get()))
        result = client.list_peers()
        self.output.insert(tk.END, f"LIST_PEERS: {result}\n")
        self.output.see(tk.END)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    ClientGUI().run()
