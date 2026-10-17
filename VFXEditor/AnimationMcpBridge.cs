using Lumina.Excel.Sheets;
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using VfxEditor.Data;
using VfxEditor.Interop.Havok;
using VfxEditor.PapFormat;
using VfxEditor.PapFormat.Motion;
using VfxEditor.Select;
using VfxEditor.Select.Tabs.Emotes;
using VfxEditor.Spawn;
using VfxEditor.Utils.Gltf;

namespace VfxEditor;

/// <summary>Loopback-only game-data inspection/export and scoped actor animation control.</summary>
public sealed class AnimationMcpBridge : IDisposable {
    public const int Port = 42072;
    private const int ProtocolVersion = 2;
    private readonly TcpListener Listener = new( IPAddress.Loopback, Port );
    private readonly CancellationTokenSource Stop = new();
    private readonly Task Server;
    private readonly SemaphoreSlim RequestLock = new( 1, 1 );
    private readonly ActorAnimationController ActorController = new();
    public string ExportRoot { get; }

    public AnimationMcpBridge() {
        ExportRoot = Path.Combine( Plugin.RootLocation, "McpExports" );
        Directory.CreateDirectory( ExportRoot );
        Listener.Start( 4 );
        Server = Task.Run( ServerLoop );
    }

    private async Task ServerLoop() {
        while( !Stop.IsCancellationRequested ) {
            try {
                var client = await Listener.AcceptTcpClientAsync( Stop.Token );
                _ = Task.Run( () => Handle( client ), Stop.Token );
            }
            catch( OperationCanceledException ) { break; }
            catch( Exception e ) { Dalamud.Error( e, "VFXEditor MCP listener failed" ); }
        }
    }

    private async Task Handle( TcpClient client ) {
        using( client ) {
            object response;
            var acquired = false;
            try {
                await RequestLock.WaitAsync( Stop.Token );
                acquired = true;
                using var reader = new StreamReader( client.GetStream(), Encoding.UTF8, false, 4096, true );
                var line = await reader.ReadLineAsync( Stop.Token );
                if( line == null || Encoding.UTF8.GetByteCount( line ) > 16384 ) throw new InvalidOperationException( "Invalid request size." );
                using var request = JsonDocument.Parse( line );
                response = await Dispatch( request.RootElement );
            }
            catch( Exception e ) { response = new { ok = false, error = e.GetBaseException().Message }; }
            finally { if( acquired ) RequestLock.Release(); }

            var bytes = Encoding.UTF8.GetBytes( JsonSerializer.Serialize( response ) + "\n" );
            await client.GetStream().WriteAsync( bytes, Stop.Token );
        }
    }

    private Task<object> OnFramework( Func<object> action ) => Dalamud.Framework.RunOnFrameworkThread( action );

    private async Task<object> Dispatch( JsonElement request ) {
        var command = request.GetProperty( "command" ).GetString();
        return command switch {
            "health" => await OnFramework( () => Health() ),
            "search_pap" => await OnFramework( () => SearchPap(
                request.GetProperty( "query" ).GetString() ?? "",
                request.GetProperty( "raceCode" ).GetString() ?? "c0801",
                request.TryGetProperty( "includeCommon", out var common ) && common.GetBoolean() ) ),
            "inspect_pap" => await OnFramework( () => WithPap( request.GetProperty( "papPath" ).GetString()!, InspectPap ) ),
            "inspect_actor_sleeve_skeleton" => await OnFramework( ActorSkeletonInspector.InspectSleeveBones ),
            "inspect_glove_bone_bindings" => await OnFramework( ActorSkeletonInspector.InspectGloveBoneBindings ),
            "export_animation" => await OnFramework( () => WithPap( request.GetProperty( "papPath" ).GetString()!, file =>
                ExportAnimation( file, request ) ) ),
            "play_actor_animation" => await OnFramework( () => PlayActorAnimation( request ) ),
            "get_actor_animation_state" => await OnFramework( () => ActorController.State() ),
            "freeze_actor_animation" => await FreezeActorAnimation( request ),
            "resume_actor_animation" => await ResumeActorAnimation(),
            "reset_actor_animation" => await ResetActorAnimation(),
            _ => throw new InvalidOperationException( "Unknown command." )
        };
    }

    private object Health() {
        var capability = ActorController.GetFreezeCapability();
        return new { ok = true, protocolVersion = ProtocolVersion, pluginVersion = "0.3.5-staging", readOnlyGameData = true,
            exportRoot = ExportRoot, writesGameFiles = false, replacesResources = false, controlsActors = true,
            inspectsLiveSleeveSkeleton = true, inspectsGloveBoneBindings = false,
            actorControlScope = "ActionTimeline override plus Brio-backed GPose main body partial 0 / control 0 scrub",
            canPlay = capability.IsGpose,
            playUnavailableReason = capability.IsGpose ? null : "Actor animation playback is restricted to GPose.",
            isGpose = capability.IsGpose, canFreeze = capability.CanFreeze, freezeBackend = capability.Backend,
            freezeUnavailableReason = capability.UnavailableReason, brioApiAvailable = capability.BrioApiAvailable,
            brioApiVersion = capability.BrioApiVersion,
            brioManagesActor = capability.BrioManagesActor, brioSpeed = capability.BrioSpeed };
    }

    private object PlayActorAnimation( JsonElement request ) {
        var forceReplay = request.TryGetProperty( "forceReplay", out var force ) && force.GetBoolean();
        uint id = 0;
        string path = null;
        if( request.TryGetProperty( "timelineId", out var idValue ) && idValue.ValueKind != JsonValueKind.Null ) {
            var candidate = idValue.GetInt32();
            if( candidate is < 1 or > ushort.MaxValue ) throw new InvalidOperationException( "timelineId must be between 1 and 65535." );
            id = (uint)candidate;
        }
        if( request.TryGetProperty( "tmbPath", out var pathValue ) && pathValue.ValueKind != JsonValueKind.Null ) {
            path = pathValue.GetString()?.Trim().Replace( '\\', '/' );
            if( string.IsNullOrWhiteSpace( path ) || path.Length > 255 || !path.EndsWith( ".tmb", StringComparison.OrdinalIgnoreCase ) ||
                Path.IsPathRooted( path ) || path.Contains( ".." ) )
                throw new InvalidOperationException( "tmbPath must be an internal game .tmb path." );
            var resolved = TmbSpawn.GetIdFromTmbPath( path );
            if( resolved == 0 ) throw new InvalidOperationException( "The TMB path does not resolve to an ActionTimeline ID. Arbitrary PAP paths are not directly playable." );
            if( id != 0 && id != resolved ) throw new InvalidOperationException( $"timelineId {id} and tmbPath resolve to different timelines ({resolved})." );
            id = resolved;
        }
        if( id == 0 ) throw new InvalidOperationException( "Provide timelineId or a resolvable internal tmbPath; PAP paths are not accepted." );
        var row = Dalamud.DataManager.GetExcelSheet<ActionTimeline>().GetRowOrDefault( id );
        if( row == null ) throw new InvalidOperationException( $"ActionTimeline {id} was not found." );
        path ??= $"chara/action/{row.Value.Key}.tmb";
        return ActorController.Play( (ushort)id, path, forceReplay );
    }

    private async Task<object> FreezeActorAnimation( JsonElement request ) {
        var frame = request.GetProperty( "frame" ).GetInt32();
        var fps = request.TryGetProperty( "fps", out var fpsValue ) ? fpsValue.GetSingle() : 30f;
        if( frame < 0 ) throw new InvalidOperationException( "frame must be non-negative and is interpreted as zero-based." );
        if( !float.IsFinite( fps ) || fps is < 1f or > 240f ) throw new InvalidOperationException( "fps must be finite and between 1 and 240." );
        // Refuse unsupported normal-mode/native freezing before waiting for or touching Havok state.
        await OnFramework( () => ActorController.RequireFreezeAvailable() );
        await OnFramework( () => ActorController.EnsureTimelineEvaluatingForFreeze() );
        // The game creates Havok controls asynchronously after PlayTimeline. Wait until slot 0 is the
        // requested timeline and the same main control/binding/animation is observed on two ticks.
        var ready = false;
        for( var attempt = 0; attempt < AnimationFreezePolicy.MaxReadyWaitTicks && !ready; attempt++ ) {
            await Dalamud.Framework.RunOnTick( () => { }, delayTicks: 1 );
            ready = (bool)await OnFramework( () => ActorController.MainControlIsReady() );
        }
        if( !ready ) throw new TimeoutException( $"Timed out waiting {AnimationFreezePolicy.MaxReadyWaitTicks} framework ticks for the requested slot-0 timeline, a synchronized 30fps timeline/control clock, and a stable main body control." );
        await OnFramework( () => ActorController.ValidateFrame( frame, fps ) );
        try {
            await OnFramework( () => ActorController.BeginFreeze() );
            // OverallSpeed changes before Havok propagates the pause to each control. Brio's own
            // scrub path waits several ticks; use the actual control PlaybackSpeed as the gate.
            var paused = false;
            for( var attempt = 0; attempt < 30 && !paused; attempt++ ) {
                await Dalamud.Framework.RunOnTick( () => { }, delayTicks: 1 );
                paused = (bool)await OnFramework( () => ActorController.MainControlIsPaused() );
            }
            if( !paused ) throw new TimeoutException( "Timed out waiting 30 framework ticks for main body control PlaybackSpeed to reach zero." );
            // Brio's scrubber writes only hkaAnimationControl.LocalTime after pausing. Apply the
            // same narrow write, then allow at most three one-tick attempts if native evaluation
            // asynchronously replaces it. Identity checks prevent writing a replacement control.
            for( var attempt = 0; attempt < AnimationFreezePolicy.MaxSeekAttempts; attempt++ ) {
                await OnFramework( () => ActorController.SeekFrame( frame, fps ) );
                await Dalamud.Framework.RunOnTick( () => { }, delayTicks: 1 );
                var matches = (bool)await OnFramework( () => ActorController.FreezeReadbackMatches() );
                if( matches ) return await OnFramework( () => ActorController.VerifyFreeze() );
            }
            return await OnFramework( () => ActorController.VerifyFreeze() );
        }
        catch {
            await OnFramework( () => { ActorController.AbortFreeze(); return new object(); } );
            throw;
        }
    }

    private async Task<object> ResumeActorAnimation() {
        var before = (float)await OnFramework( () => ActorController.BeginResume() );
        await Dalamud.Framework.RunOnTick( () => { }, delayTicks: 4 );
        return await OnFramework( () => ActorController.VerifyResume( before ) );
    }

    private async Task<object> ResetActorAnimation() {
        await OnFramework( () => { ActorController.ReleasePauseBeforeReset(); return new object(); } );
        await Dalamud.Framework.RunOnTick( () => { }, delayTicks: 2 );
        await OnFramework( () => ActorController.ApplyReset() );
        await Dalamud.Framework.RunOnTick( () => { }, delayTicks: 8 );
        return await OnFramework( () => ActorController.VerifyReset() );
    }

    private static object SearchPap( string query, string raceCode, bool includeCommon ) {
        query = query.Trim();
        if( query.Length is < 1 or > 100 ) throw new InvalidOperationException( "query must contain 1-100 characters." );
        if( !Regex.IsMatch( raceCode, "^c[0-9]{4}$", RegexOptions.CultureInvariant ) ||
            !SelectDataUtils.CharacterRaces.Any( x => x.Id == raceCode ) )
            throw new InvalidOperationException( "raceCode must be a supported playable-race code such as c0801." );

        var matches = new List<object>();
        foreach( var row in Dalamud.DataManager.GetExcelSheet<Emote>() ) {
            if( string.IsNullOrEmpty( row.Name.ExtractText() ) ) continue;
            var emote = new EmoteRow( row );
            if( !( emote.Name.Contains( query, StringComparison.OrdinalIgnoreCase ) ||
                   emote.Command.Contains( query, StringComparison.OrdinalIgnoreCase ) ||
                   emote.Keys.Any( x => x.Item1.Contains( query, StringComparison.OrdinalIgnoreCase ) ) ) ) continue;
            foreach( var (key, type) in emote.Items ) {
                if( type == EmoteRowType.Normal ) {
                    var path = SelectDataUtils.GetSkeletonPath( raceCode, $"bt_common/{key}.pap" );
                    if( Dalamud.DataManager.FileExists( path ) ) matches.Add( new { source = "emote", emote.RowId, emote.Name, emote.Command, key, raceCode, papPath = path } );
                }
                else if( type == EmoteRowType.PerJob ) {
                    foreach( var (job, folder) in SelectDataUtils.JobAnimationIds ) {
                        var path = SelectDataUtils.GetSkeletonPath( raceCode, $"{folder}/{key}.pap" );
                        if( Dalamud.DataManager.FileExists( path ) ) matches.Add( new { source = "emote", emote.RowId, emote.Name, emote.Command, key, job, raceCode, papPath = path } );
                    }
                }
            }
            if( matches.Count >= 100 ) break;
        }

        if( includeCommon && matches.Count < 100 && File.Exists( SelectDataUtils.CommonPapPath ) ) {
            foreach( var path in File.ReadLines( SelectDataUtils.CommonPapPath )
                .Where( x => x.Contains( query, StringComparison.OrdinalIgnoreCase ) ).Take( 100 - matches.Count ) )
                matches.Add( new { source = "common", papPath = path } );
        }
        return new { ok = true, query, raceCode, count = matches.Count, truncated = matches.Count >= 100, matches };
    }

    private static unsafe object WithPap( string papPath, Func<PapFile, object> action ) {
        if( string.IsNullOrWhiteSpace( papPath ) || papPath.Length > 255 || !papPath.EndsWith( ".pap", StringComparison.OrdinalIgnoreCase ) ||
            Path.IsPathRooted( papPath ) || papPath.Contains( ".." ) )
            throw new InvalidOperationException( "papPath must be an internal game .pap path." );
        papPath = papPath.Replace( '\\', '/' );
        if( !Dalamud.DataManager.FileExists( papPath ) ) throw new FileNotFoundException( "Game PAP was not found.", papPath );

        var tempDir = Path.Combine( Plugin.RootLocation, "McpExports", ".temp" );
        Directory.CreateDirectory( tempDir );
        var stem = Guid.NewGuid().ToString( "N" );
        var hkx = Path.Combine( tempDir, stem + ".hkx" );
        var sklbHkx = Path.Combine( tempDir, stem + "_sklb.hkx" );
        PapFile file = null;
        try {
            var resource = Dalamud.DataManager.GetFile( papPath ) ?? throw new InvalidOperationException( "PAP data could not be read." );
            using var stream = new MemoryStream( resource.Data );
            using var reader = new BinaryReader( stream );
            file = new PapFile( reader, papPath, hkx, true, false, true );
            if( file.MotionData?.AnimationContainer == null ) throw new InvalidOperationException( "PAP contains no readable Havok animation container." );
            var skeletonPath = file.MotionData.GetDefaultSkeletonPath();
            var skeleton = Dalamud.DataManager.GetFile<SimpleSklb>( skeletonPath ) ?? throw new FileNotFoundException( "Matching game skeleton was not found.", skeletonPath );
            file.MotionData.InitializeSkeleton( skeleton );
            if( file.MotionData.Skeleton == null ) throw new InvalidOperationException( "Matching skeleton could not be initialized." );
            return action( file );
        }
        finally {
            file?.Dispose();
            TryDelete( hkx );
            TryDelete( sklbHkx );
        }
    }

    private static unsafe object InspectPap( PapFile file ) {
        SheetData.InitMotionTimelines();
        var skeleton = file.MotionData.Skeleton;
        var bones = Enumerable.Range( 0, skeleton->Bones.Length ).Select( i => skeleton->Bones[i].Name.String ).ToArray();
        var animations = file.Animations.Select( ( animation, index ) => {
            var motion = MotionFor( file, animation.HavokIndex );
            var hasTimeline = SheetData.MotionTimelines.TryGetValue( animation.GetName(), out var timeline );
            return new { index, name = animation.GetName(), havokIndex = animation.HavokIndex, papType = animation.GetPapType(),
                durationSeconds = motion.Duration, sampleCount30Fps = Math.Max( 1, ( int )MathF.Round( motion.Duration * 30f ) + 1 ),
                loop = hasTimeline ? timeline.Loop : (bool?)null,
                transformTrackCount = motion.Binding->TransformTrackToBoneIndices.Length,
                unanimatedBoneCount = motion.GetUnanimatedBones().Count,
                mismatchedSkeleton = motion.HasMismatchedSkeleton() };
        } ).ToArray();
        return new { ok = true, papPath = file.SourcePath, skeletonPath = file.MotionData.GetDefaultSkeletonPath(),
            modelType = file.ModelType.Value.ToString(), modelId = file.ModelId.Value, variant = file.Variant.Value,
            boneCount = bones.Length, bones, animationCount = animations.Length, animations };
    }

    private unsafe object ExportAnimation( PapFile file, JsonElement request ) {
        var outputName = request.GetProperty( "outputName" ).GetString() ?? "animation.gltf";
        if( !Regex.IsMatch( outputName, "^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\\.gltf$", RegexOptions.CultureInvariant ) )
            throw new InvalidOperationException( "outputName must be a simple ASCII .gltf filename without directories." );
        var skip = request.TryGetProperty( "skipUnanimated", out var skipValue ) && skipValue.GetBoolean();
        var animationIndex = request.TryGetProperty( "animationIndex", out var indexValue ) && indexValue.ValueKind != JsonValueKind.Null ? indexValue.GetInt32() : -1;
        var animationName = request.TryGetProperty( "animationName", out var nameValue ) && nameValue.ValueKind != JsonValueKind.Null ? nameValue.GetString() : null;
        if( animationIndex < 0 && string.IsNullOrWhiteSpace( animationName ) ) throw new InvalidOperationException( "Provide animationName or animationIndex." );

        var candidates = file.Animations.Select( ( value, index ) => (value, index) );
        var selected = animationIndex >= 0
            ? candidates.FirstOrDefault( x => x.index == animationIndex )
            : candidates.FirstOrDefault( x => string.Equals( x.value.GetName(), animationName, StringComparison.OrdinalIgnoreCase ) );
        if( selected.value == null ) throw new InvalidOperationException( "Animation was not found in this PAP." );
        var motion = MotionFor( file, selected.value.HavokIndex );
        if( motion.HasMismatchedSkeleton() ) throw new InvalidOperationException( "Animation tracks do not fit the selected matching skeleton; export refused." );

        var output = Path.Combine( ExportRoot, outputName );
        GltfAnimation.ExportAnimation( file.MotionData.Skeleton, [selected.value.GetName()], [motion], skip, output );
        var bin = Path.ChangeExtension( output, ".bin" );
        if( !File.Exists( output ) || !File.Exists( bin ) ) throw new IOException( "glTF export did not produce both .gltf and .bin files." );

        using var gltf = JsonDocument.Parse( File.ReadAllBytes( output ) );
        var channels = gltf.RootElement.GetProperty( "animations" )[0].GetProperty( "channels" );
        var animatedNodes = channels.EnumerateArray().Select( x => x.GetProperty( "target" ).GetProperty( "node" ).GetInt32() ).Distinct().Count();
        var animatedIndices = Enumerable.Range( 0, motion.Binding->TransformTrackToBoneIndices.Length )
            .Select( i => ( int )motion.Binding->TransformTrackToBoneIndices[i] ).Where( i => i >= 0 ).Distinct().ToArray();
        var skeleton = file.MotionData.Skeleton;
        var animatedBones = animatedIndices.Where( i => i < skeleton->Bones.Length ).Select( i => skeleton->Bones[i].Name.String ).ToArray();
        SheetData.InitMotionTimelines();
        var hasTimeline = SheetData.MotionTimelines.TryGetValue( selected.value.GetName(), out var timeline );

        return new { ok = true, papPath = file.SourcePath, animationIndex = selected.index, animationName = selected.value.GetName(),
            havokIndex = selected.value.HavokIndex, durationSeconds = motion.Duration,
            sampleCount30Fps = Math.Max( 1, ( int )MathF.Round( motion.Duration * 30f ) + 1 ), loop = hasTimeline ? timeline.Loop : (bool?)null,
            skeletonPath = file.MotionData.GetDefaultSkeletonPath(), boneCount = skeleton->Bones.Length,
            sourceTransformTrackCount = motion.Binding->TransformTrackToBoneIndices.Length, sourceAnimatedBoneCount = animatedBones.Length,
            sourceAnimatedBones = animatedBones, skipUnanimated = skip, exportedChannelCount = channels.GetArrayLength(), exportedAnimatedNodeCount = animatedNodes,
            gltfPath = output, binPath = bin, gltfBytes = new FileInfo( output ).Length, binBytes = new FileInfo( bin ).Length,
            gltfSha256 = Sha256( output ), binSha256 = Sha256( bin ), writesGameFiles = false };
    }

    private static PapMotion MotionFor( PapFile file, int havokIndex ) {
        if( havokIndex < 0 || havokIndex >= file.MotionData.ReadOnlyMotions.Count )
            throw new InvalidOperationException( $"Havok motion index {havokIndex} is out of range." );
        return file.MotionData.ReadOnlyMotions[havokIndex];
    }

    private static string Sha256( string path ) => Convert.ToHexString( SHA256.HashData( File.ReadAllBytes( path ) ) ).ToLowerInvariant();
    private static void TryDelete( string path ) { try { File.Delete( path ); } catch { } }

    public void Dispose() {
        Stop.Cancel();
        Listener.Stop();
        try { Server.Wait( 1000 ); } catch { }
        RequestLock.Dispose();
        ActorController.Dispose();
        Stop.Dispose();
    }
}
