//+------------------------------------------------------------------+
//| APEX MT5 Server Clock Publisher                                  |
//| Read-only EA: publishes broker server UTC offset for Python.      |
//| It never sends, modifies, or cancels trading orders.               |
//+------------------------------------------------------------------+
#property strict
#property version   "1.00"
#property description "Publishes TimeTradeServer()-TimeGMT() to APEX; no trading."

input int PublishIntervalSeconds = 5;

void PublishClock()
{
   datetime server_time = TimeTradeServer();
   datetime gmt_time = TimeGMT();

   if(server_time <= 0 || gmt_time <= 0)
   {
      Print("APEX_CLOCK: server/GMT clock unavailable; offset not published");
      return;
   }

   long offset_seconds = (long)server_time - (long)gmt_time;
   if(offset_seconds < -43200 || offset_seconds > 50400)
   {
      PrintFormat("APEX_CLOCK: implausible server offset %I64d; offset not published",
                  offset_seconds);
      return;
   }

   int handle = FileOpen("apex_mt5_clock_offset.txt",
                         FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(handle == INVALID_HANDLE)
   {
      PrintFormat("APEX_CLOCK: FileOpen failed, error=%d", GetLastError());
      return;
   }

   string payload = StringFormat(
      "schema_version=1\nserver=%s\noffset_seconds=%I64d\n"
      "observed_server_epoch=%I64d\nobserved_gmt_epoch=%I64d\n",
      AccountInfoString(ACCOUNT_SERVER),
      offset_seconds,
      (long)server_time,
      (long)gmt_time
   );
   FileWriteString(handle, payload);
   FileFlush(handle);
   FileClose(handle);
}

int OnInit()
{
   int interval = PublishIntervalSeconds;
   if(interval < 1)
      interval = 1;
   if(!EventSetTimer(interval))
   {
      PrintFormat("APEX_CLOCK: EventSetTimer failed, error=%d", GetLastError());
      return INIT_FAILED;
   }
   PublishClock();
   Print("APEX_CLOCK: publisher active; read-only, no order operations");
   return INIT_SUCCEEDED;
}

void OnTimer()
{
   PublishClock();
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}
