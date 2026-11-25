"""Lightweight E-Commerce REST API with v1 (baseline) and v2 (regressed) modes."""

from __future__ import annotations

import os
import threading
import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, Response, status
from pydantic import AliasChoices, BaseModel, Field


class RegisterRequest(BaseModel):
    """User registration payload."""

    username: str
    password: str
    email: Optional[str] = None


class LoginRequest(BaseModel):
    """User login credentials."""

    username: str
    password: str


class CartItemRequest(BaseModel):
    """Payload to add a product to the shopping cart."""

    product_id: str = Field(validation_alias=AliasChoices("product_id", "id"))
    quantity: int = Field(default=1, ge=1)


class DemoStore:
    """Thread-safe in-memory store for products, users, carts, and orders."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.users: Dict[str, str] = {}
        self.products: Dict[str, Dict[str, Any]] = {
            "prod-1": {
                "id": "prod-1",
                "name": "Mechanical Keyboard",
                "price": 100.0,
                "inventory": 25,
            },
            "prod-2": {
                "id": "prod-2",
                "name": "USB-C Hub",
                "price": 50.0,
                "inventory": 40,
            },
            "prod-3": {
                "id": "prod-3",
                "name": "4K Monitor",
                "price": 300.0,
                "inventory": 10,
            },
        }
        self.carts: Dict[str, Dict[str, int]] = {}
        self.order_counter: int = 0

    def register_user(self, username: str, password: str) -> None:
        with self._lock:
            self.users[username] = password

    def list_products(self, include_inventory: bool) -> List[Dict[str, Any]]:
        with self._lock:
            results: List[Dict[str, Any]] = []
            for product in self.products.values():
                item = {
                    "id": product["id"],
                    "name": product["name"],
                    "price": product["price"],
                }
                if include_inventory:
                    item["inventory"] = product["inventory"]
                results.append(item)
            return results

    def add_to_cart(self, cart_key: str, product_id: str, quantity: int) -> Dict[str, int]:
        with self._lock:
            if product_id not in self.products:
                raise KeyError(product_id)
            cart = self.carts.setdefault(cart_key, {})
            cart[product_id] = cart.get(product_id, 0) + quantity
            return dict(cart)

    def checkout(self, cart_key: str, apply_buggy_surcharge: bool) -> Dict[str, Any]:
        with self._lock:
            cart = self.carts.get(cart_key, {})
            if not cart:
                raise ValueError("Cart is empty.")

            items_summary: List[Dict[str, Any]] = []
            subtotal = 0.0

            for product_id, qty in cart.items():
                product = self.products[product_id]
                if product["inventory"] < qty:
                    raise ValueError(f"Insufficient inventory for {product_id}.")
                product["inventory"] -= qty
                line_total = float(product["price"]) * qty
                subtotal += line_total
                items_summary.append(
                    {
                        "product_id": product_id,
                        "name": product["name"],
                        "quantity": qty,
                        "unit_price": product["price"],
                    }
                )

            self.order_counter += 1
            order_id = f"ORD-{self.order_counter}"
            self.carts[cart_key] = {}

            if apply_buggy_surcharge:
                total_price = round(subtotal * 1.15, 2)
                transaction_id = str(uuid.uuid4())
            else:
                total_price = round(subtotal, 2)
                transaction_id = f"txn-v1-{self.order_counter}"

            return {
                "order_id": order_id,
                "items": items_summary,
                "total_price": total_price,
                "transaction_id": transaction_id,
                "status": "confirmed",
            }


def _extract_cart_key(authorization: Optional[str]) -> str:
    """Derive a session cart key from the Bearer token or default bucket."""
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or "default"
    return "default"


def create_app(mode: Optional[str] = None) -> FastAPI:
    """Create the E-Commerce FastAPI application in v1 or v2 mode."""
    resolved_mode = (mode or os.environ.get("ASSAY_DEMO_MODE", "v1")).strip().lower()
    is_v2 = resolved_mode in {"v2", "v2_regressed"}

    fastapi_app = FastAPI(
        title="Assay E-Commerce Demo API",
        version="2.0.0" if is_v2 else "1.0.0",
    )
    store = DemoStore()

    @fastapi_app.get("/health")
    async def health() -> Dict[str, str]:
        return {"status": "ok", "mode": "v2_regressed" if is_v2 else "v1_baseline"}

    @fastapi_app.post("/api/auth/register", status_code=status.HTTP_201_CREATED)
    async def register(payload: RegisterRequest, response: Response) -> Dict[str, str]:
        store.register_user(payload.username, payload.password)
        if is_v2:
            response.status_code = status.HTTP_200_OK
        else:
            response.status_code = status.HTTP_201_CREATED
        return {"status": "registered", "username": payload.username}

    @fastapi_app.post("/api/auth/login")
    async def login(payload: LoginRequest) -> Dict[str, str]:
        return {
            "token": f"demo-token-{payload.username}",
            "token_type": "bearer",
        }

    @fastapi_app.get("/api/products")
    async def list_products() -> List[Dict[str, Any]]:
        return store.list_products(include_inventory=not is_v2)

    @fastapi_app.post("/api/cart/items")
    async def add_cart_item(
        payload: CartItemRequest,
        authorization: Optional[str] = Header(default=None),
    ) -> Dict[str, Any]:
        cart_key = _extract_cart_key(authorization)
        try:
            cart = store.add_to_cart(cart_key, payload.product_id, payload.quantity)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Product '{payload.product_id}' not found.",
            ) from exc
        return {"status": "added", "cart": cart}

    @fastapi_app.post("/api/cart/checkout")
    async def checkout(
        authorization: Optional[str] = Header(default=None),
    ) -> Dict[str, Any]:
        cart_key = _extract_cart_key(authorization)
        try:
            return store.checkout(cart_key, apply_buggy_surcharge=is_v2)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

    return fastapi_app


app = create_app()
