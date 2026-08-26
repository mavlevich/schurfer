# ruff: noqa
import subprocess
import time

scripts = [
    "run_lead_lag.py",
    "pump_lead_lag.py",
    "pump_fuel.py",
    "pump_orderflow.py",
    "pump_lifecycle.py",
    "pump_seasonality.py",
    "pump_funding.py",
    "mean_reversion.py",
]

print("🚀 Запускаем полный цикл аналитики (Quant Methodology v2) 🚀\n")

for script in scripts:
    print(f"{'='*60}")
    print(f"▶ Выполняется: {script} ...")
    print(f"{'='*60}")

    start = time.time()
    try:
        # uv run python apps/analytics/<script>
        result = subprocess.run(
            ["uv", "run", "python", f"apps/analytics/{script}"], capture_output=True, text=True
        )
        if result.returncode == 0:
            print(result.stdout)
        else:
            print(f"❌ Ошибка в {script}:\n{result.stderr}")
    except Exception as e:
        print(f"Критическая ошибка запуска {script}: {e}")

    print(f"⏱ Завершено за {time.time() - start:.1f} сек.\n")

print("✅ Полный цикл завершен.")
