using Dalamud.Game.ClientState.Objects.Types;
using FFXIVClientStructs.FFXIV.Client.Game.Character;
using FFXIVClientStructs.FFXIV.Client.Graphics.Scene;
using FFXIVClientStructs.Havok.Animation.Rig;
using FFXIVClientStructs.Havok.Common.Base.Math.QsTransform;
using System;
using System.Collections.Generic;
using System.Linq;
using System.Numerics;

namespace VfxEditor;

/// <summary>
/// Narrow, read-only inspection of the sleeve bones used by the current experiment.
/// This intentionally reads pose arrays directly: it never calls hkaPose Access*/Sync*
/// methods, because those accessors may update Havok cache/dirty state.
/// </summary>
public static unsafe class ActorSkeletonInspector {
    private static readonly HashSet<string> TargetNames = new( StringComparer.Ordinal ) {
        "j_sako_l", "j_sako_r", "j_ude_a_l", "j_ude_a_r", "j_ude_b_l", "j_ude_b_r", "j_te_l", "j_te_r",
        "sl_a", "sl_b", "sl_c", "sl_d", "sl_e",
        "sr_a", "sr_b", "sr_c", "sr_d", "sr_e",
        "r387_sl_Aa", "r387_sl_Ab", "r387_sl_Ac", "r387_sl_Ad", "r387_sl_Ae",
        "r387_sl_Ba", "r387_sl_Bb", "r387_sl_Bc", "r387_sl_Bd", "r387_sl_Be",
        "r387_sl_Ca", "r387_sl_Cb", "r387_sl_Cc", "r387_sl_Cd", "r387_sl_Ce",
        "r387_sr_Aa", "r387_sr_Ab", "r387_sr_Ac", "r387_sr_Ad", "r387_sr_Ae",
        "r387_sr_Ba", "r387_sr_Bb", "r387_sr_Bc", "r387_sr_Bd", "r387_sr_Be",
        "r387_sr_Ca", "r387_sr_Cb", "r387_sr_Cc", "r387_sr_Cd", "r387_sr_Ce"
    };

    public static object InspectGloveBoneBindings() {
        return new {
            ok = true,
            enabled = false,
            mode = "disabled before actor/native access",
            unavailableReason = "Disabled after a confirmed CN-client AccessViolation in the experimental native glove-binding inspector. No actor or native memory was accessed.",
        };
    }

    public static object InspectSleeveBones() {
        var characterBase = CurrentGposeCharacterBase( out var actor );
        var gameSkeleton = characterBase->Skeleton;
        if( gameSkeleton == null || gameSkeleton->PartialSkeletonCount == 0 || gameSkeleton->PartialSkeletons == null )
            throw new InvalidOperationException( "The current GPose target has no readable render skeleton." );
        if( gameSkeleton->PartialSkeletonCount > 16 )
            throw new InvalidOperationException( $"Implausible partial-skeleton count {gameSkeleton->PartialSkeletonCount}; inspection refused." );

        var partials = new List<object>();
        var matches = new List<object>();
        var foundNameSet = new HashSet<string>( StringComparer.Ordinal );
        for( var partialIndex = 0; partialIndex < gameSkeleton->PartialSkeletonCount; partialIndex++ ) {
            var partial = &gameSkeleton->PartialSkeletons[partialIndex];
            var poseSummaries = new List<object>();
            for( var poseIndex = 0; poseIndex < 4; poseIndex++ ) {
                var candidate = partial->GetHavokPose( poseIndex );
                poseSummaries.Add( candidate == null || candidate->Skeleton == null
                    ? new { poseIndex, present = false, boneCount = (int?)null, localInSync = (bool?)null, modelInSync = (bool?)null }
                    : new { poseIndex, present = true, boneCount = (int?)candidate->Skeleton->Bones.Length,
                        localInSync = (bool?)( candidate->LocalInSync != 0 ), modelInSync = (bool?)( candidate->ModelInSync != 0 ) } );
            }

            // Brio's reviewed Skeleton.GetBestPose() and runtime posing paths select pose slot 0.
            // Keep that same selection, but report all slots above rather than hiding the choice.
            var pose = partial->GetHavokPose( 0 );
            var selectedBoneCount = 0;
            if( pose != null && pose->Skeleton != null ) {
                var skeleton = pose->Skeleton;
                var boneCount = skeleton->Bones.Length;
                if( boneCount < 0 || boneCount > 2048 || skeleton->Bones.Data == null ||
                    skeleton->ParentIndices.Data == null || skeleton->ParentIndices.Length != boneCount ||
                    skeleton->ReferencePose.Data == null || skeleton->ReferencePose.Length != boneCount )
                    throw new InvalidOperationException( $"Partial {partialIndex} pose 0 has inconsistent skeleton arrays." );

                var referenceModels = BuildReferenceModels( skeleton );
                var localReadable = pose->LocalInSync != 0 && pose->LocalPose.Data != null && pose->LocalPose.Length == boneCount;
                var modelReadable = pose->ModelInSync != 0 && pose->ModelPose.Data != null && pose->ModelPose.Length == boneCount;
                for( var boneIndex = 0; boneIndex < boneCount; boneIndex++ ) {
                    var name = skeleton->Bones[boneIndex].Name.String;
                    if( name == null || !TargetNames.Contains( name ) ) continue;
                    selectedBoneCount++;
                    foundNameSet.Add( name );
                    matches.Add( new {
                        name, partialIndex, poseIndex = 0, boneIndex,
                        parentIndex = skeleton->ParentIndices[boneIndex],
                        parentName = ParentName( skeleton, boneIndex ),
                        referenceLocal = Trs( skeleton->ReferencePose[boneIndex] ),
                        referenceModel = Trs( referenceModels[boneIndex] ),
                        currentLocal = localReadable ? Trs( pose->LocalPose[boneIndex] ) : null,
                        currentModel = modelReadable ? Trs( pose->ModelPose[boneIndex] ) : null,
                        currentLocalReadStatus = localReadable ? "direct-synchronized-array" : "unavailable-without-mutating-sync",
                        currentModelReadStatus = modelReadable ? "direct-synchronized-array" : "unavailable-without-mutating-sync"
                    } );
                }
            }

            partials.Add( new {
                partialIndex,
                connectedParentBoneIndex = partial->ConnectedParentBoneIndex,
                connectedBoneIndex = partial->ConnectedBoneIndex,
                selectedPoseIndex = 0,
                selectedTargetBoneCount = selectedBoneCount,
                poses = poseSummaries
            } );
        }

        var foundNames = foundNameSet.OrderBy( value => value, StringComparer.Ordinal ).ToArray();
        var missingNames = TargetNames.Except( foundNames, StringComparer.Ordinal ).OrderBy( value => value, StringComparer.Ordinal ).ToArray();
        return new {
            ok = true,
            mode = "live actor memory, read-only direct arrays",
            actor = new { name = actor.Name.ToString(), gameObjectId = $"0x{Convert.ToUInt64( actor.GameObjectId ):X}" },
            isGpose = true,
            partialSkeletonCount = gameSkeleton->PartialSkeletonCount,
            poseSelection = "pose slot 0, matching reviewed Brio GetBestPose/runtime posing implementation",
            coordinateSystem = new {
                source = "FFXIV Havok hkQsTransformf",
                handedness = "right-handed",
                axes = "native X lateral, Y vertical/up, Z depth; no Blender/glTF axis conversion applied",
                units = "game character model units; treated as metres by the established Blender/glTF workflow",
                quaternionOrder = "x,y,z,w",
                referenceLocalSpace = "local to parent; partial roots are local to their partial-skeleton model space",
                referenceModelSpace = "managed composition of referenceLocal through this partial's parentIndices",
                currentModelSpace = "direct hkaPose.ModelPose for the same partial; no actor/world transform applied"
            },
            safety = new {
                writesActorMemory = false, callsHavokSyncOrAccessors = false, changesAnimation = false,
                changesPhysics = false, changesResources = false, changesMods = false
            },
            requestedBoneCount = TargetNames.Count,
            foundNames,
            missingNames,
            partials,
            bones = matches,
            warnings = new[] {
                "A direct current pose is returned only when Havok marks that array synchronized; the inspector will not synchronize or dirty the pose as a side effect.",
                "Partial-to-base attachment is reported by connected indices but is not folded into each partial's model-space transforms.",
                "This is a single framework-thread sample after the game's latest completed updates, not a physics trajectory or collision diagnosis."
            }
        };
    }

    private static CharacterBase* CurrentGposeCharacterBase( out IGameObject actor ) {
        if( !Dalamud.ClientState.IsLoggedIn )
            throw new InvalidOperationException( "The client is not logged in; no actor memory was accessed." );
        if( !Plugin.InGpose )
            throw new InvalidOperationException( "Live sleeve-skeleton inspection is restricted to GPose to avoid actor-selection ambiguity." );

        actor = Plugin.PlayerObject;
        if( actor == null || actor.Address == nint.Zero )
            throw new InvalidOperationException( "No valid current GPose target actor is available." );

        var character = (Character*)actor.Address;
        var drawObject = character->GameObject.DrawObject;
        if( drawObject == null || drawObject->Object.GetObjectType() != ObjectType.CharacterBase )
            throw new InvalidOperationException( "The current GPose target has no CharacterBase draw object." );
        return (CharacterBase*)drawObject;
    }

    private static string ParentName( hkaSkeleton* skeleton, int boneIndex ) {
        var parent = skeleton->ParentIndices[boneIndex];
        return parent >= 0 && parent < skeleton->Bones.Length ? skeleton->Bones[parent].Name.String : null;
    }

    private static Matrix4x4[] BuildReferenceModels( hkaSkeleton* skeleton ) {
        var count = skeleton->Bones.Length;
        var models = new Matrix4x4[count];
        for( var index = 0; index < count; index++ ) {
            var parent = skeleton->ParentIndices[index];
            if( parent >= index || parent < -1 )
                throw new InvalidOperationException( $"Bone {index} has invalid/non-topological parent index {parent}." );
            var local = Matrix( skeleton->ReferencePose[index] );
            models[index] = parent < 0 ? local : local * models[parent];
        }
        return models;
    }

    private static Matrix4x4 Matrix( hkQsTransformf value ) {
        var scale = new Vector3( value.Scale.X, value.Scale.Y, value.Scale.Z );
        var rotation = Quaternion.Normalize( new Quaternion( value.Rotation.X, value.Rotation.Y, value.Rotation.Z, value.Rotation.W ) );
        var translation = new Vector3( value.Translation.X, value.Translation.Y, value.Translation.Z );
        return Matrix4x4.CreateScale( scale ) * Matrix4x4.CreateFromQuaternion( rotation ) * Matrix4x4.CreateTranslation( translation );
    }

    private static object Trs( hkQsTransformf value ) => new {
        translation = new[] { value.Translation.X, value.Translation.Y, value.Translation.Z },
        rotation = new[] { value.Rotation.X, value.Rotation.Y, value.Rotation.Z, value.Rotation.W },
        scale = new[] { value.Scale.X, value.Scale.Y, value.Scale.Z }
    };

    private static object Trs( Matrix4x4 value ) {
        if( !Matrix4x4.Decompose( value, out var scale, out var rotation, out var translation ) )
            throw new InvalidOperationException( "A reference model matrix could not be decomposed to finite TRS." );
        rotation = Quaternion.Normalize( rotation );
        return new {
            translation = new[] { translation.X, translation.Y, translation.Z },
            rotation = new[] { rotation.X, rotation.Y, rotation.Z, rotation.W },
            scale = new[] { scale.X, scale.Y, scale.Z }
        };
    }
}
