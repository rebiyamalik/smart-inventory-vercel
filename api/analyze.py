
import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler


def num(value):
    try:
        result = float(value or 0)
        if result < 0:
            raise ValueError("Negative values are not allowed.")
        return result
    except (TypeError, ValueError):
        raise ValueError("Sales, stock and prices must be non-negative numbers.")


class handler(BaseHTTPRequestHandler):
    def send_json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4_000_000:
                return self.send_json(413, {"error": "Request too large or empty."})

            payload = json.loads(self.rfile.read(length))
            rows = payload.get("rows", [])
            threshold = max(0, float(payload.get("threshold", 25)))
            safety_days = max(0, float(payload.get("safety_days", 7)))

            if not isinstance(rows, list) or not rows or len(rows) > 10000:
                return self.send_json(400, {"error": "Provide 1–10,000 data rows."})

            required = ["Product", "Units_Sold", "Stock", "Unit_Price", "Unit_Cost"]
            totals = {}
            detail = []

            for row in rows:
                missing = [key for key in required if key not in row]
                if missing:
                    return self.send_json(400, {
                        "error": "Missing columns: " + ", ".join(missing)
                    })

                product = str(row["Product"]).strip() or "Unnamed product"
                units = num(row["Units_Sold"])
                stock = num(row["Stock"])
                price = num(row["Unit_Price"])
                cost = num(row["Unit_Cost"])
                lead = num(row.get("Lead_Time_Days", 0))

                revenue = units * price
                cogs = units * cost
                profit = revenue - cogs

                item = {
                    "Product": product,
                    "Units_Sold": units,
                    "Stock": stock,
                    "Revenue": round(revenue, 2),
                    "Cost_of_Goods": round(cogs, 2),
                    "Gross_Profit": round(profit, 2),
                    "Profit_Margin_%": round(profit / revenue * 100, 2) if revenue else 0
                }
                detail.append(item)

                if product not in totals:
                    totals[product] = {
                        "Product": product, "Units_Sold": 0,
                        "Stock": stock, "Revenue": 0,
                        "Cost_of_Goods": 0, "Gross_Profit": 0,
                        "Lead_Time_Days": lead,
                        "Reorder_Level": row.get("Reorder_Level")
                    }

                p = totals[product]
                p["Units_Sold"] += units
                p["Stock"] = stock
                p["Revenue"] += revenue
                p["Cost_of_Goods"] += cogs
                p["Gross_Profit"] += profit
                p["Lead_Time_Days"] = max(p["Lead_Time_Days"], lead)

                if row.get("Reorder_Level") not in (None, ""):
                    p["Reorder_Level"] = num(row["Reorder_Level"])

            products = []
            for p in totals.values():
                rev = p["Revenue"]
                p["Profit_Margin_%"] = round(
                    p["Gross_Profit"] / rev * 100, 2
                ) if rev else 0

                # Estimate assumes uploaded sales represent about 30 days.
                proxy = p["Units_Sold"] / 30
                supplied_level = p["Reorder_Level"]
                reorder = (
                    num(supplied_level) if supplied_level not in (None, "")
                    else proxy * (p["Lead_Time_Days"] + safety_days)
                )
                p["Avg_Daily_Sales_Proxy"] = round(proxy, 2)
                p["Reorder_Point_Estimate"] = round(reorder, 0)
                p["Stock_Status"] = "LOW STOCK" if p["Stock"] <= threshold else "OK"
                p["Reorder_Alert"] = p["Stock"] < reorder

                for key in ["Revenue", "Cost_of_Goods", "Gross_Profit"]:
                    p[key] = round(p[key], 2)
                products.append(p)

            products.sort(key=lambda p: p["Revenue"], reverse=True)
            low_stock = [p for p in products if p["Stock_Status"] == "LOW STOCK"]

            revenue = sum(p["Revenue"] for p in products)
            profit = sum(p["Gross_Profit"] for p in products)

            summary = {
                "row_count": len(detail),
                "product_count": len(products),
                "total_revenue": round(revenue, 2),
                "gross_profit": round(profit, 2),
                "gross_margin": round(profit / revenue * 100, 2) if revenue else 0,
                "low_stock_count": len(low_stock),
                "top_product": products[0]["Product"] if products else "N/A"
            }

            recommendations = ""
            key = os.environ.get("GEMINI_API_KEY", "")
            model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

            if key:
                prompt = (
                    "Act as a retail inventory analyst. Give 3 concise, "
                    "practical recommendations using only these results. "
                    "Mention limitations and do not claim this is a validated "
                    "demand forecast.\nSummary: "
                    + json.dumps(summary)
                    + "\nProducts: "
                    + json.dumps(products[:20])
                )
                request = urllib.request.Request(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                    data=json.dumps({
                        "contents": [{"parts": [{"text": prompt}]}]
                    }).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                try:
                    with urllib.request.urlopen(request, timeout=20) as response:
                        result = json.loads(response.read().decode())
                    recommendations = result["candidates"][0]["content"]["parts"][0]["text"]
                except Exception:
                    recommendations = (
                        "Gemini was unavailable. Check your API key, model, "
                        "quota, and Vercel function logs."
                    )

            return self.send_json(200, {
                "summary": summary,
                "products": products,
                "detail": detail,
                "low_stock": low_stock,
                "recommendations": recommendations
            })

        except ValueError as e:
            self.send_json(400, {"error": str(e)})
        except Exception:
            self.send_json(500, {"error": "Analysis failed. Check your input and server logs."})

