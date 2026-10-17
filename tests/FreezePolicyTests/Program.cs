using VfxEditor;

static void Check( bool condition, string message ) {
    if( !condition ) throw new InvalidOperationException( message );
}

var identity = new AnimationFreezePolicy.ControlIdentity( (nint)1, (nint)2, (nint)3, 3.6666667f );
var replacement = new AnimationFreezePolicy.ControlIdentity( (nint)4, (nint)2, (nint)3, 3.6666667f );

Check( AnimationFreezePolicy.RequiredReadyObservations == 2, "Readiness must require two observations." );
Check( AnimationFreezePolicy.MaxReadyWaitTicks == 120, "Readiness wait must remain bounded." );
Check( AnimationFreezePolicy.MaxSeekAttempts == 3, "Seek retries must remain bounded at three writes." );
Check( AnimationFreezePolicy.NextReadyObservation( 3753, 3, default, identity, 0 ) == 0,
    "A previous slot-0 timeline must not count as ready." );
Check( AnimationFreezePolicy.NextReadyObservation( 3753, 3753, default, identity, 0 ) == 1,
    "The first matching identity observation must not be ready." );
Check( AnimationFreezePolicy.NextReadyObservation( 3753, 3753, identity, identity, 1 ) == 2,
    "The second stable identity observation must become ready." );
Check( AnimationFreezePolicy.NextReadyObservation( 3753, 3753, identity, replacement, 2 ) == 1,
    "A replacement control must restart readiness." );
Check( AnimationFreezePolicy.NextReadyObservation( 695, 695, identity, identity, 2, false ) == 0,
    "A stable pointer whose local time does not match the requested timeline clock must not remain ready." );

Check( !AnimationFreezePolicy.IsTimelineClockSynchronized( 1.122035f, 0f, 0f ),
    "The observed stale hug control must not match the newly requested cheer clock." );
Check( AnimationFreezePolicy.IsTimelineClockSynchronized( 1.5765896f, 47.29769f, 47.29769f ),
    "The observed live cheer control must match its 30fps scheduler clock." );
Check( !AnimationFreezePolicy.IsTimelineClockSynchronized( 5.6666665f, 10.213f, 10.213f ),
    "An endpoint control retained while the scheduler restarted must not be accepted." );
Check( !AnimationFreezePolicy.IsTimelineClockSynchronized( 1f, 30f, 31f ),
    "Divergent scheduler/controller clocks must not be accepted." );
Check( AnimationFreezePolicy.ResetTimelineTarget( 3753 ) == 3753,
    "Reset must target the actual pre-takeover slot rather than hard-coded timeline 3." );
Check( AnimationFreezePolicy.ResetTimelineTarget( 0 ) == 3,
    "An empty pre-takeover slot uses the safe base fallback." );

var tolerance = AnimationFreezePolicy.TimeTolerance( 30f );
Check( MathF.Abs( tolerance - 1f / 120f ) < 0.000001f, "30fps tolerance is incorrect." );
Check( AnimationFreezePolicy.IsFreezeReadbackValid(
        identity, identity, 1f, 1f + tolerance * 0.9f, 30f, 0f, 0f ),
    "A paused, matching control inside tolerance must validate." );
Check( !AnimationFreezePolicy.IsFreezeReadbackValid(
        identity, identity, 1f, 1f + tolerance * 1.1f, 30f, 0f, 0f ),
    "A time outside tolerance must fail." );
Check( !AnimationFreezePolicy.IsFreezeReadbackValid(
        identity, replacement, 1f, 1f, 30f, 0f, 0f ),
    "A replacement control must fail readback." );
Check( !AnimationFreezePolicy.IsFreezeReadbackValid(
        identity, identity, 1f, 1f, 30f, 1f, 0f ),
    "Non-zero overall speed must fail readback." );
Check( !AnimationFreezePolicy.IsFreezeReadbackValid(
        identity, identity, 1f, 1f, 30f, 0f, 1f ),
    "Non-zero control speed must fail readback." );
Check( !AnimationFreezePolicy.IsFreezeReadbackValid(
        identity, identity, 1f, float.NaN, 30f, 0f, 0f ),
    "Non-finite readback must fail." );
Check( AnimationFreezePolicy.IsFrameReadbackValid( 30, 1f, 30f ),
    "An exact frame readback must validate." );
Check( !AnimationFreezePolicy.IsFrameReadbackValid( 30, 1.02f, 30f ),
    "A neighboring frame must not validate as the requested frame." );

Console.WriteLine( "FreezePolicyTests: all checks passed." );
