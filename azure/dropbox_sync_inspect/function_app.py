import azure.functions as func

app = func.FunctionApp()

@app.timer_trigger(
    schedule="0 0 */6 * * *",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def dropbox_sync(timer: func.TimerRequest) -> None:
    print("Dropbox ? Azure sync triggered.")
